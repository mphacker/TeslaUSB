import { useEffect, useState } from "preact/hooks";
import { Shell } from "../components/Shell";
import { Icon } from "../components/Icon";
import { api, ApiError } from "../api/client";
import type { Analytics, DaySummary } from "../api/types";

interface HubData {
  analytics: Analytics;
  days: DaySummary[];
  /** Clip count from the first page; `clipsHasMore` if the page was full. */
  clipCount: number;
  clipsHasMore: boolean;
  /** True when speed_unit pref selects imperial (miles). */
  imperial: boolean;
}

const nf = new Intl.NumberFormat("en-US");

/** Format a count with a correctly pluralised unit (e.g. "1 day", "3 days"). */
function plur(n: number, singular: string, plural = `${singular}s`): string {
  return `${nf.format(n)} ${n === 1 ? singular : plural}`;
}

function formatDistance(meters: number, imperial: boolean): string {
  if (imperial) {
    const mi = meters / 1609.344;
    return `${nf.format(Math.round(mi * 10) / 10)} mi`;
  }
  const km = meters / 1000;
  return `${nf.format(Math.round(km * 10) / 10)} km`;
}

interface HealthRow {
  status: "ok" | "warn" | "error" | "unknown";
  name: string;
  value: string;
}

/**
 * The media hub (home/landing screen).
 *
 * Read-only by construction — it consumes only webd's catalog read API
 * (`/api/analytics`, `/api/days`, `/api/clips`, `/api/settings`) and issues no
 * mutations. It reuses the legacy parity components (device-status card,
 * settings-section, the System-Health rows grid, the metric-tile grid, and the
 * media-pill nav tiles) so it matches the captured baseline's visual language.
 */
export function MediaHub() {
  const [data, setData] = useState<HubData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      try {
        const [analytics, days, clipsPage, settings] = await Promise.all([
          api.analytics(ctrl.signal),
          api.days(ctrl.signal),
          api.clips({ limit: 500 }, ctrl.signal),
          api.settings(ctrl.signal),
        ]);
        const unit = settings.find((p) => p.key === "speed_unit")?.value ?? "";
        setData({
          analytics,
          days,
          clipCount: clipsPage.items.length,
          clipsHasMore: clipsPage.next_cursor !== null,
          imperial: /mph|mi/i.test(unit),
        });
      } catch (err) {
        if (ctrl.signal.aborted) return;
        const msg =
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : (err as Error).message;
        setError(msg);
      }
    })();
    return () => ctrl.abort();
  }, []);

  return (
    <Shell active="media">
      <div class="container" data-screen="media-hub">
        {/* Device status card — verbatim parity element. */}
        <div class="device-status-card device-status-unknown">
          <div class="device-status-header">
            <span class="status-dot status-unknown" />
            <div class="device-status-info">
              <strong>TeslaUSB</strong>
              <p>Read-only catalog browser — your drive&apos;s trips, events and clips.</p>
            </div>
          </div>
        </div>

        {error && (
          <div class="settings-section" id="hub-error-section">
            <div class="section-content">
              <p class="hub-error" role="alert" data-testid="hub-error">
                Could not load the catalog: {error}
              </p>
            </div>
          </div>
        )}

        {data && (
          <>
            <HealthSection data={data} />
            <MetricsSection data={data} />
            <RecentDaysSection days={data.days} imperial={data.imperial} />
          </>
        )}

        <BrowseSection />
      </div>
    </Shell>
  );
}

function HealthSection({ data }: { data: HubData }) {
  const { analytics, days, clipCount, clipsHasMore } = data;
  const rows: HealthRow[] = [
    {
      status: "ok",
      name: "Video Indexer",
      value: `${nf.format(clipCount)}${clipsHasMore ? "+" : ""} clips indexed`,
    },
    { status: "ok", name: "Trips", value: `${plur(analytics.total_trips, "trip")} catalogued` },
    { status: "ok", name: "Events", value: plur(analytics.total_events, "event") },
    { status: "ok", name: "Driving Days", value: `${plur(days.length, "day")} with trips` },
    { status: "ok", name: "Catalog", value: "Online — read-only (webd)" },
  ];
  return (
    <details class="settings-section" id="system-health-section" open>
      <summary>System Health</summary>
      <div class="section-content">
        <div id="system-health-card" data-testid="system-health-card">
          <p class="health-overall" data-testid="health-overall">
            <span class="health-dot-cell ok" aria-hidden="true" />
            <span>Catalog online — read-only</span>
          </p>
          <div class="health-rows">
            {rows.map((r) => (
              <>
                <span class={`health-dot-cell ${r.status}`} aria-label={r.status} />
                <div class="health-name">{r.name}</div>
                <div class="health-value">{r.value}</div>
              </>
            ))}
          </div>
        </div>
      </div>
    </details>
  );
}

function MetricsSection({ data }: { data: HubData }) {
  const { analytics, days, clipCount, clipsHasMore, imperial } = data;
  const tiles = [
    { label: "Trips", value: nf.format(analytics.total_trips), detail: plur(days.length, "driving day") },
    { label: "Distance", value: formatDistance(analytics.total_distance_m, imperial), detail: "total catalogued" },
    { label: "Events", value: nf.format(analytics.total_events), detail: plur(analytics.events_by_type.length, "type") },
    { label: "Clips", value: `${nf.format(clipCount)}${clipsHasMore ? "+" : ""}`, detail: "camera clips" },
    { label: "Days", value: nf.format(days.length), detail: "with driving" },
  ];
  return (
    <details class="settings-section" id="catalog-metrics-section" open>
      <summary>Catalog</summary>
      <div class="section-content">
        <div class="metric-grid" data-testid="catalog-metrics">
          {tiles.map((t) => (
            <div class="metric-tile" data-metric={t.label.toLowerCase()}>
              <div class="metric-label">{t.label}</div>
              <div class="metric-value">{t.value}</div>
              <div class="metric-detail">{t.detail}</div>
            </div>
          ))}
        </div>
      </div>
    </details>
  );
}

function RecentDaysSection({
  days,
  imperial,
}: {
  days: DaySummary[];
  imperial: boolean;
}) {
  const recent = days.slice(0, 7);
  return (
    <details class="settings-section" id="recent-days-section" open>
      <summary>Recent driving days</summary>
      <div class="section-content">
        {recent.length === 0 ? (
          <p class="health-value">No driving days catalogued yet.</p>
        ) : (
          <div class="health-rows" data-testid="recent-days">
            {recent.map((d) => (
              <>
                <span class="health-dot-cell ok" aria-hidden="true" />
                <div class="health-name">{d.day}</div>
                <div class="health-value">
                  {plur(d.trip_count, "trip")} · {plur(d.event_count, "event")} ·{" "}
                  {formatDistance(d.distance_m, imperial)}
                </div>
              </>
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

interface Tile {
  href: string;
  icon: string;
  label: string;
}

const BROWSE_TILES: Tile[] = [
  { href: "/", icon: "map-pin", label: "Map" },
  { href: "/analytics", icon: "bar-chart-2", label: "Analytics" },
  { href: "/events", icon: "alert-triangle", label: "Events" },
  { href: "/media", icon: "image", label: "Clips" },
  { href: "/cloud", icon: "cloud", label: "Cloud" },
];

function BrowseSection() {
  return (
    <details class="settings-section" id="browse-section" open>
      <summary>Browse</summary>
      <div class="section-content">
        <div class="media-pills" data-testid="browse-tiles">
          {BROWSE_TILES.map((t) => (
            <a href={t.href} class="media-pill">
              <Icon name={t.icon} />
              {t.label}
            </a>
          ))}
        </div>
      </div>
    </details>
  );
}
