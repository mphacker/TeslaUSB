import { useEffect, useRef, useState } from "preact/hooks";
import { Icon } from "../components/Icon";
import { api, ApiError } from "../api/client";
import type { Analytics as AnalyticsData } from "../api/types";
import { AnalyticsCharts, type AnalyticsChartModel } from "../charts/controller";
import "../styles/analytics.css";

const METERS_PER_MILE = 1609.344;
const EM_DASH = "\u2014";

/**
 * The analytics screen (route `/analytics`, Shell active "analytics") — a parity
 * carry of the legacy Flask **Storage Analytics Dashboard** (`analytics.html`),
 * carrying its `analytics.css` (scoped) so the layout/typography land exactly.
 *
 * Data boundary (the reason this isn't a 1:1 pixel carry of the populated Flask
 * baseline): webd's read-only catalog API serves ONLY `GET /api/analytics`
 * (trip/event aggregates) — it exposes NO storage-probe, partition, video-file,
 * or folder data, and this lane may not add a webd endpoint. So, exactly as the
 * sibling MediaHub screen does for the system-probe sections it can't read, the
 * storage-analytics half of the page renders the legacy DEGRADED state (a
 * legacy-styled `.alert`) rather than fabricating drive/partition/folder numbers
 * (ASK-FIRST: "render the legacy degraded/loading state rather than fabricating
 * data"). The half webd CAN back renders live:
 *   · Driving Statistics — Total Distance / Trips / Events from /api/analytics;
 *     the speed/FSD/drive-time fields webd does not serve show the legacy "—".
 *   · Charts — Events-by-Type + Trips-by-Day (Chart.js), realising the legacy
 *     analytics chart intent on the live aggregates, driven imperatively by
 *     {@link AnalyticsCharts} via a ref/effect (Chart.js is not a Preact lib).
 */

function humanize(type: string): string {
  return type
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function toChartModel(a: AnalyticsData): AnalyticsChartModel {
  return {
    events: {
      labels: a.events_by_type.map((e) => humanize(e.type)),
      values: a.events_by_type.map((e) => e.count),
    },
    trips: {
      labels: a.trips_by_day.map((d) => d.day),
      values: a.trips_by_day.map((d) => d.count),
    },
  };
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : (err as Error).message;
}

function buildId(): string {
  return (
    (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__ ??
    "dev"
  );
}

export function Analytics() {
  const eventsCanvasRef = useRef<HTMLCanvasElement>(null);
  const tripsCanvasRef = useRef<HTMLCanvasElement>(null);
  const chartsRef = useRef<AnalyticsCharts | null>(null);

  const [data, setData] = useState<AnalyticsData | null>(null);
  const [error, setError] = useState<string | null>(null);

  // ── Mount: one live read of the read-only aggregates. ──
  useEffect(() => {
    const ac = new AbortController();
    (async () => {
      try {
        const a = await api.analytics(ac.signal);
        setData(a);
      } catch (err) {
        if (ac.signal.aborted) return;
        setError(errMessage(err));
      }
    })();
    return () => ac.abort();
  }, []);

  // ── Charts lifecycle: create once the data + canvases exist, destroy on
  //    unmount. The canvases are only in the DOM after `data` arrives, and
  //    Preact runs effects after committing that render, so the refs are set. ──
  useEffect(() => {
    if (!data) return;
    const ec = eventsCanvasRef.current;
    const tc = tripsCanvasRef.current;
    if (!ec || !tc) return;
    const charts = new AnalyticsCharts({ events: ec, trips: tc }, buildId());
    chartsRef.current = charts;
    charts.render(toChartModel(data));
    return () => {
      charts.destroy();
      chartsRef.current = null;
    };
  }, [data]);

  const miles =
    data != null
      ? `${(data.total_distance_m / METERS_PER_MILE).toFixed(1)} mi`
      : EM_DASH;
  const trips = data != null ? String(data.total_trips) : EM_DASH;
  const events = data != null ? String(data.total_events) : EM_DASH;

  return (
    <div
      class="container"
      id="analyticsDashboard"
      data-page="analytics"
      data-screen="analytics"
    >
      <h2>
        <Icon name="bar-chart-2" /> Storage Analytics Dashboard
      </h2>

      {error ? (
        // Genuine read failure → the legacy "analytics is none" degraded copy,
        // verbatim. (Recovers automatically on the next mount.)
        <div class="alert alert-warning" role="status" data-testid="analytics-unavailable">
          <strong>
            <Icon name="alert-triangle" /> Analytics temporarily unavailable
          </strong>
          <div>
            The mapping database could not be read. The dashboard will recover
            automatically once the indexer is healthy.
          </div>
        </div>
      ) : (
        <>
          {/* Storage-analytics half: degraded by design — webd's read-only
              catalog API exposes no storage/partition/video/folder metrics, and
              fabricating them is forbidden (ASK-FIRST). Legacy `.alert` styling
              keeps it recognisably the legacy dashboard in its read-only state. */}
          <div
            class="alert alert-warning"
            role="status"
            data-testid="storage-degraded"
          >
            <strong>
              <Icon name="alert-triangle" /> Storage analytics unavailable
            </strong>
            <div>
              Drive-usage, partition, recording-estimate, video-file and
              folder-breakdown metrics are not exposed by the read-only catalog
              API. Trip and event analytics are shown below.
            </div>
          </div>

          {/* Driving Statistics — live aggregates from /api/analytics; the
              speed/FSD/drive-time fields webd does not serve show the legacy "—". */}
          <div class="analytics-section" id="drivingStatsSection">
            <h3>
              <Icon name="map-pin" /> Driving Statistics
            </h3>
            <p class="section-description">
              GPS and telemetry derived from indexed dashcam clips.{" "}
              <a href="/">Open Map →</a>
            </p>
            <div id="drivingStatsContent">
              <div
                class="analytics-grid"
                id="drivingStatsGrid"
                data-testid="driving-stats"
              >
                <div class="analytics-card">
                  <div class="stat-row">
                    <span class="stat-label">Total Distance</span>
                    <span class="stat-value" id="dsTotalDist">
                      {miles}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Total Drive Time</span>
                    <span class="stat-value" id="dsTotalTime">
                      {EM_DASH}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Total Trips</span>
                    <span class="stat-value" id="dsTripCount">
                      {trips}
                    </span>
                  </div>
                </div>
                <div class="analytics-card">
                  <div class="stat-row">
                    <span class="stat-label">Avg Speed</span>
                    <span class="stat-value" id="dsAvgSpeed">
                      {EM_DASH}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Max Speed</span>
                    <span class="stat-value" id="dsMaxSpeed">
                      {EM_DASH}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">FSD Usage</span>
                    <span class="stat-value" id="dsFsdPct">
                      {EM_DASH}
                    </span>
                  </div>
                </div>
                <div class="analytics-card">
                  <div class="stat-row">
                    <span class="stat-label">Total Events</span>
                    <span class="stat-value" id="dsEventCount">
                      {events}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Warnings/Critical</span>
                    <span class="stat-value" id="dsWarnCount">
                      {EM_DASH}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Events per 100 mi</span>
                    <span class="stat-value" id="dsEvPer100">
                      {EM_DASH}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Charts — net-new, live from /api/analytics. Canvases mount only
              once data has arrived so Chart.js always has real datasets. */}
          <div class="analytics-section" id="analyticsChartsSection">
            <h3>
              <Icon name="bar-chart-2" /> Event &amp; Trip Charts
            </h3>
            {data == null ? (
              <p class="section-description" data-testid="charts-loading">
                Loading analytics…
              </p>
            ) : (
              <div class="analytics-charts">
                <div class="analytics-card chart-card">
                  <h4>Events by Type</h4>
                  <div class="chart-canvas-wrap">
                    <canvas id="eventsByTypeChart" ref={eventsCanvasRef} />
                  </div>
                </div>
                <div class="analytics-card chart-card">
                  <h4>Trips by Day</h4>
                  <div class="chart-canvas-wrap">
                    <canvas id="tripsByDayChart" ref={tripsCanvasRef} />
                  </div>
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
