import { Icon } from "./Icon";

/**
 * The media-section pill sub-nav (parity port of the legacy
 * `media_hub_nav.html`). Rendered at the top of every media-section screen
 * (Chimes / Music / Boombox / Shows / Wraps / Plates).
 *
 * Each pill is a real in-app link to the screen's route, matching the v1
 * paths so direct visits and the rendered `href`s stay faithful:
 *   chimes → /media (the SPA's lock-chimes screen, where v1 `/media/` landed)
 *   music  → /music         boombox → /boombox      shows → /light_shows
 *   wraps  → /wraps         plates  → /license_plates
 *
 * The styling comes entirely from the carried-over legacy stylesheet
 * (`.media-pills` / `.media-pill` / `.media-pill.active`), so the bar renders
 * pixel-faithfully with no new CSS.
 */

export type MediaPillKey =
  | "chimes"
  | "music"
  | "boombox"
  | "shows"
  | "wraps"
  | "plates";

interface PillDef {
  key: MediaPillKey;
  href: string;
  icon: string;
  label: string;
}

// Order mirrors the v1 `media_hub_nav.html`.
const PILLS: PillDef[] = [
  { key: "chimes", href: "/media", icon: "bell", label: "Chimes" },
  { key: "music", href: "/music", icon: "music", label: "Music" },
  { key: "boombox", href: "/boombox", icon: "megaphone", label: "Boombox" },
  { key: "shows", href: "/light_shows", icon: "sparkles", label: "Shows" },
  { key: "wraps", href: "/wraps", icon: "palette", label: "Wraps" },
  { key: "plates", href: "/license_plates", icon: "image", label: "Plates" },
];

export function MediaPills({ active }: { active: MediaPillKey }) {
  return (
    <div class="media-pills" data-testid="media-pills">
      {PILLS.map((p) => {
        const isActive = p.key === active;
        return (
          <a
            key={p.key}
            href={p.href}
            class={isActive ? "media-pill active" : "media-pill"}
            data-pill={p.key}
            aria-current={isActive ? "page" : undefined}
          >
            <Icon name={p.icon} />
            {p.label}
          </a>
        );
      })}
    </div>
  );
}
