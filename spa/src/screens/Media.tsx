import { useEffect, useState } from "preact/hooks";
import { Icon } from "../components/Icon";
import { api, ApiError } from "../api/client";
import type { Clip, EventItem, Page } from "../api/types";
import "../styles/media.css";

/**
 * The Media-section landing screen (route `/media`, Shell active "media") — the
 * entry hub for the media feature set (spa.md §3 "Home / media hub: landing,
 * nav, media tiles", legacy `index.html` + `media_hub_nav.html`).
 *
 * It is the launchpad into the per-feature media screens (event player, boombox,
 * music, light shows, lock chimes, license plates, wraps, cloud archive) and
 * surfaces the most recent recordings + events so the hub reflects live device
 * activity rather than a static menu.
 *
 * Data boundary: webd is **read-only**. The hub reads only the catalog list
 * endpoints it is permitted to call — `GET /api/clips` and `GET /api/events`
 * (both cursor pages, ascending by id; the hub sorts them newest-first
 * client-side for the "recent" lists). The per-feature manager screens (boombox,
 * music, …) are owned by sibling parity lanes; until they land, their tiles
 * resolve through the router's graceful client-side placeholder (no dead-ends,
 * no full reload) — exactly the convention the router documents.
 *
 * Wiring proof: the screen publishes `window.__TESLAUSB_MEDIA_HOOKS__` (build id
 * + the live recent-list counts) so the UAT can prove THIS module — not a stale
 * bundle — produced the rendered DOM.
 */

const EM_DASH = "\u2014";
const RECENT_CLIPS = 6;
const RECENT_EVENTS = 6;
const PAGE_SIZE = 200;
// Safety cap so a pathological catalog can't drive an unbounded fetch loop.
const MAX_PAGES = 100;

/** A media feature tile: a card linking into a per-feature screen. */
interface FeatureTile {
  key: string;
  href: string;
  icon: string;
  label: string;
  description: string;
  /** True when the target screen is implemented in this SPA (vs. a placeholder). */
  ready: boolean;
}

// Mirrors the legacy media hub navigation (`media_hub_nav.html`) + the spa.md §3
// media checklist. Only the event player + cloud archive resolve to real screens
// today; the rest land in sibling lanes and fall back to the router placeholder.
const TILES: FeatureTile[] = [
  {
    key: "events",
    href: "/events",
    icon: "video",
    label: "Clips & Events",
    description: "Watch dashcam and Sentry recordings with the telemetry HUD.",
    ready: true,
  },
  {
    key: "boombox",
    href: "/boombox",
    icon: "megaphone",
    label: "Boombox",
    description: "Upload, trim and assign external-speaker audio.",
    ready: false,
  },
  {
    key: "music",
    href: "/music",
    icon: "music",
    label: "Music",
    description: "Manage the on-device music library.",
    ready: false,
  },
  {
    key: "shows",
    href: "/light-shows",
    icon: "sparkles",
    label: "Light Shows",
    description: "Manage custom light shows.",
    ready: false,
  },
  {
    key: "chimes",
    href: "/lock-chimes",
    icon: "bell",
    label: "Lock Chimes",
    description: "Manage lock chimes and the chime scheduler.",
    ready: false,
  },
  {
    key: "plates",
    href: "/license-plates",
    icon: "image",
    label: "License Plates",
    description: "Manage stored license plates.",
    ready: false,
  },
  {
    key: "wraps",
    href: "/wraps",
    icon: "palette",
    label: "Wraps",
    description: "Manage vehicle wraps.",
    ready: false,
  },
  {
    key: "cloud",
    href: "/cloud",
    icon: "cloud",
    label: "Cloud Archive",
    description: "Browse, queue and sync archived clips.",
    ready: false,
  },
];

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Format an epoch-second time as the legacy "YYYY-MM-DD hh:mm AM/PM". */
function fmtDateTime(epochSec: number): string {
  const d = new Date(epochSec * 1000);
  let h = d.getHours();
  const ampm = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(h)}:${pad2(d.getMinutes())} ${ampm}`
  );
}

/** Humanise a snake/lower-case enum into a Title-Case label. */
function humanize(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Humanise a CamelCase folder class (e.g. "SentryClips" → "Sentry Clips"). */
function humanizeFolder(s: string): string {
  return s.replace(/([a-z])([A-Z])/g, "$1 $2");
}

/** Format a clip's duration (seconds) as "M:SS", or the em-dash when absent. */
function fmtDuration(seconds: number | null): string {
  if (seconds == null) return EM_DASH;
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${pad2(s)}`;
}

/** Map an event severity (1 = most severe) to a media severity-dot class. */
function severityClass(sev: number | null): string {
  if (sev == null) return "media-sev-unknown";
  if (sev <= 1) return "media-sev-error";
  if (sev === 2) return "media-sev-warn";
  return "media-sev-ok";
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : (err as Error).message;
}

/**
 * Drain a cursor-paginated endpoint to the end. webd's list endpoints page
 * ASCENDING by id with no descending/recent query, so the only correct way to
 * find the *newest* rows is to read every page and sort by timestamp client-side
 * (a single `limit` page would miss newer rows beyond it). Bounded by
 * {@link MAX_PAGES} so a pathological catalog can't loop forever.
 */
async function drain<T>(
  fetchPage: (after: number | undefined, signal: AbortSignal) => Promise<Page<T>>,
  signal: AbortSignal,
): Promise<T[]> {
  const all: T[] = [];
  let after: number | undefined = undefined;
  for (let i = 0; i < MAX_PAGES; i++) {
    const page = await fetchPage(after, signal);
    all.push(...page.items);
    if (page.next_cursor == null || page.items.length === 0) break;
    after = page.next_cursor;
  }
  return all;
}

function buildId(): string {
  return (
    (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__ ??
    "dev"
  );
}

interface RecentData {
  clips: Clip[];
  events: EventItem[];
}

export function Media() {
  const [data, setData] = useState<RecentData | null>(null);
  const [error, setError] = useState<string | null>(null);

  // ── Mount: read the two read-only catalog lists in full. The endpoints page
  //    ascending by id with no recent/descending query, so drain every page and
  //    sort newest-first client-side to get a correct "recent" head. ──
  useEffect(() => {
    const ac = new AbortController();
    (async () => {
      try {
        const [clipItems, eventItems] = await Promise.all([
          drain((after, s) => api.clips({ after, limit: PAGE_SIZE }, s), ac.signal),
          drain((after, s) => api.events({ after, limit: PAGE_SIZE }, s), ac.signal),
        ]);
        const clips = [...clipItems]
          .sort((a, b) => b.started_at - a.started_at)
          .slice(0, RECENT_CLIPS);
        const events = eventItems
          .filter((e) => e.clip_id != null)
          .sort((a, b) => b.t - a.t)
          .slice(0, RECENT_EVENTS);
        setData({ clips, events });
      } catch (err) {
        if (ac.signal.aborted) return;
        setError(errMessage(err));
      }
    })();
    return () => ac.abort();
  }, []);

  // ── Wiring-proof hook: prove THIS module produced the live DOM. ──
  useEffect(() => {
    (
      window as unknown as {
        __TESLAUSB_MEDIA_HOOKS__?: {
          build: string;
          clipCount: number;
          eventCount: number;
        };
      }
    ).__TESLAUSB_MEDIA_HOOKS__ = {
      build: buildId(),
      clipCount: data?.clips.length ?? 0,
      eventCount: data?.events.length ?? 0,
    };
  }, [data]);

  return (
    <div class="container" data-page="media" data-screen="media">
      <h2>
        <Icon name="music" /> Media
      </h2>
      <p class="section-description">
        Your dashcam library and on-device media. Open a recording, or jump into
        a media manager.
      </p>

      {/* ── Media feature tiles (the hub nav into the per-feature screens) ── */}
      <div class="media-tiles" data-testid="media-tiles">
        {TILES.map((t) => (
          <a
            key={t.key}
            href={t.href}
            class="media-tile"
            data-feature={t.key}
            data-ready={t.ready ? "true" : "false"}
          >
            <span class="media-tile-icon" aria-hidden="true">
              <Icon name={t.icon} />
            </span>
            <span class="media-tile-body">
              <span class="media-tile-label">
                {t.label}
                {t.ready ? null : (
                  <span class="media-tile-badge">Soon</span>
                )}
              </span>
              <span class="media-tile-desc">{t.description}</span>
            </span>
            <Icon name="chevron-right" class="media-tile-chevron" />
          </a>
        ))}
      </div>

      {error ? (
        // Genuine read failure → a legacy-styled degraded alert. Recovers on the
        // next mount; the feature tiles above stay usable regardless.
        <div class="media-alert" role="status" data-testid="media-unavailable">
          <strong>
            <Icon name="alert-triangle" /> Recent activity unavailable
          </strong>
          <div>
            The media catalog could not be read. Recent clips and events will
            reappear once the indexer is healthy.
          </div>
        </div>
      ) : (
        <>
          {/* ── Recent clips ── */}
          <div class="settings-section media-recent" id="recentClipsSection">
            <div class="media-recent-header">
              <h3>
                <Icon name="video" /> Recent Clips
              </h3>
              <a href="/events" class="media-recent-link">
                Open player <Icon name="chevron-right" />
              </a>
            </div>
            <div class="section-content">
              {data == null ? (
                <p class="section-description" data-testid="clips-loading">
                  Loading recent clips…
                </p>
              ) : data.clips.length === 0 ? (
                <p class="section-description" data-testid="clips-empty">
                  No recordings have been indexed yet.
                </p>
              ) : (
                <ul class="media-list" data-testid="recent-clips">
                  {data.clips.map((c) => (
                    <li class="media-list-item" key={c.id} data-clip-id={c.id}>
                      <span class="media-list-icon" aria-hidden="true">
                        <Icon name={c.is_sentry ? "shield-alert" : "video"} />
                      </span>
                      <span class="media-list-main">
                        <span class="media-list-title">
                          {fmtDateTime(c.started_at)}
                        </span>
                        <span class="media-list-sub">
                          {humanizeFolder(c.folder_class)}
                          {c.is_sentry ? (
                            <span class="media-tag media-tag-sentry">Sentry</span>
                          ) : null}
                        </span>
                      </span>
                      <span class="media-list-meta">{fmtDuration(c.duration_s)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          {/* ── Recent events ── */}
          <div class="settings-section media-recent" id="recentEventsSection">
            <div class="media-recent-header">
              <h3>
                <Icon name="alert-circle" /> Recent Events
              </h3>
              <a href="/events" class="media-recent-link">
                Open player <Icon name="chevron-right" />
              </a>
            </div>
            <div class="section-content">
              {data == null ? (
                <p class="section-description" data-testid="events-loading">
                  Loading recent events…
                </p>
              ) : data.events.length === 0 ? (
                <p class="section-description" data-testid="events-empty">
                  No events with a playable clip have been recorded yet.
                </p>
              ) : (
                <ul class="media-list" data-testid="recent-events">
                  {data.events.map((e) => (
                    <li class="media-list-item" key={e.id} data-event-id={e.id}>
                      <span
                        class={`media-list-icon status-dot ${severityClass(e.severity)}`}
                        aria-hidden="true"
                      />
                      <span class="media-list-main">
                        <span class="media-list-title">
                          {e.description || humanize(e.type)}
                        </span>
                        <span class="media-list-sub">
                          {humanize(e.type)} · {fmtDateTime(e.t)}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
