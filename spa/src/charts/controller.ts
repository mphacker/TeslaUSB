/**
 * AnalyticsCharts — the imperative Chart.js integration for the analytics
 * screen. Chart.js is a non-Preact, canvas-driven library, so (exactly like the
 * Leaflet `map/controller.ts`) it is driven through this plain controller class
 * created/destroyed from a Preact ref/effect (see `screens/Analytics.tsx`).
 *
 * It realises the legacy analytics page's chart intent (the legacy
 * `static/js/analytics.js` carried a `chartPalette` for event/trip charts) on
 * webd's read-only `/api/analytics` aggregates:
 *   · Events by Type  → a doughnut over `events_by_type`.
 *   · Trips by Day     → a bar chart over `trips_by_day`.
 *
 * Determinism for the UAT (Chart.js can otherwise flake a zero-console gate):
 *   · `animation: false` — no async tween before a stable frame.
 *   · `maintainAspectRatio: false` + a fixed-height CSS container — never a
 *     blank zero-size canvas.
 *   · charts are destroyed-before-recreate and on unmount, so a canvas is never
 *     reused while a live Chart is still bound to it (a Chart.js console warn).
 *
 * Test hooks (`window.__TESLAUSB_ANALYTICS__`, `__TESLAUSB_ANALYTICS_HOOKS__`)
 * expose the LIVE Chart instances' data + element metadata so Playwright asserts
 * the charts actually rendered the live datasets — not merely that a `<canvas>`
 * exists (mirrors how the trip map exposes live Leaflet layers).
 */
import {
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  DoughnutController,
  Legend,
  LinearScale,
  Tooltip,
  type ChartConfiguration,
} from "chart.js";

// Register only the controllers/elements/scales actually used (tree-shaken).
Chart.register(
  DoughnutController,
  BarController,
  ArcElement,
  BarElement,
  CategoryScale,
  LinearScale,
  Tooltip,
  Legend,
);

/** Pre-formatted, render-ready chart model (the screen does the formatting). */
export interface AnalyticsChartModel {
  events: { labels: string[]; values: number[] };
  trips: { labels: string[]; values: number[] };
}

/** Canvas handles the screen wires up via refs. */
export interface AnalyticsCanvases {
  events: HTMLCanvasElement;
  trips: HTMLCanvasElement;
}

/** A live, JSON-safe snapshot of one Chart instance (read back by the UAT). */
export interface ChartSnapshot {
  type: string;
  labels: string[];
  data: number[];
  /** number of rendered datum elements (arcs / bars) in dataset 0. */
  elementCount: number;
  /** per-element "size": doughnut arc circumference, or bar height (px). */
  elementSizes: number[];
  destroyed: boolean;
}

// Parity-leaning palette (legacy analytics accent is #2196F3; warm hues follow
// the event-severity vocabulary the legacy chartPalette used).
const PALETTE = [
  "#2196F3",
  "#FF9800",
  "#F44336",
  "#4CAF50",
  "#9C27B0",
  "#00BCD4",
  "#FFC107",
  "#607D8B",
];

const ACCENT = "#2196F3";

export class AnalyticsCharts {
  private readonly canvases: AnalyticsCanvases;
  private readonly buildId: string;
  private eventsChart: Chart | null = null;
  private tripsChart: Chart | null = null;
  private rendered = false;

  constructor(canvases: AnalyticsCanvases, buildId: string) {
    this.canvases = canvases;
    this.buildId = buildId;
    this.exposeHooks();
  }

  /** Create (or update) both charts from the live model. */
  render(model: AnalyticsChartModel): void {
    this.eventsChart = this.upsertDoughnut(
      this.eventsChart,
      this.canvases.events,
      model.events,
    );
    this.tripsChart = this.upsertBar(
      this.tripsChart,
      this.canvases.trips,
      model.trips,
    );
    this.rendered = true;
  }

  // Chart.js typings are invariant over the chart-type generic, so a
  // `Chart<"doughnut", …>` is not assignable to the broad `Chart` we store; the
  // construction sites narrow via this single, contained cast.

  /** Tear down both charts and remove the test hooks. */
  destroy(): void {
    this.rendered = false;
    if (this.eventsChart) {
      this.eventsChart.destroy();
      this.eventsChart = null;
    }
    if (this.tripsChart) {
      this.tripsChart.destroy();
      this.tripsChart = null;
    }
    const w = window as unknown as Record<string, unknown>;
    if (w.__TESLAUSB_ANALYTICS__ === this) {
      delete w.__TESLAUSB_ANALYTICS__;
      delete w.__TESLAUSB_ANALYTICS_HOOKS__;
    }
  }

  // ── chart builders ──────────────────────────────────────────────────────
  private upsertDoughnut(
    existing: Chart | null,
    canvas: HTMLCanvasElement,
    d: { labels: string[]; values: number[] },
  ): Chart {
    if (existing) {
      existing.data.labels = d.labels;
      existing.data.datasets[0].data = d.values;
      existing.update("none");
      return existing;
    }
    const config: ChartConfiguration<"doughnut", number[], string> = {
      type: "doughnut",
      data: {
        labels: d.labels,
        datasets: [
          {
            data: d.values,
            backgroundColor: d.labels.map((_, i) => PALETTE[i % PALETTE.length]),
            borderWidth: 1,
          },
        ],
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: true, position: "bottom" },
          tooltip: { enabled: true },
        },
      },
    };
    return new Chart(canvas, config) as unknown as Chart;
  }

  private upsertBar(
    existing: Chart | null,
    canvas: HTMLCanvasElement,
    d: { labels: string[]; values: number[] },
  ): Chart {
    if (existing) {
      existing.data.labels = d.labels;
      existing.data.datasets[0].data = d.values;
      existing.update("none");
      return existing;
    }
    const config: ChartConfiguration<"bar", number[], string> = {
      type: "bar",
      data: {
        labels: d.labels,
        datasets: [
          {
            label: "Trips",
            data: d.values,
            backgroundColor: ACCENT,
            borderWidth: 0,
          },
        ],
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { enabled: true } },
        scales: {
          x: { grid: { display: false } },
          y: {
            beginAtZero: true,
            ticks: { precision: 0 },
          },
        },
      },
    };
    return new Chart(canvas, config) as unknown as Chart;
  }

  // ── live test hooks ───────────────────────────────────────────────────────
  private snapshot(chart: Chart | null, type: string): ChartSnapshot | null {
    if (!chart) return null;
    const ds = chart.data.datasets[0];
    const meta = chart.getDatasetMeta(0);
    const elements = meta.data ?? [];
    const elementSizes = elements.map((el) => {
      const anyEl = el as unknown as {
        circumference?: number;
        height?: number;
      };
      // doughnut arcs expose `circumference`; bars expose `height`.
      if (typeof anyEl.circumference === "number") return anyEl.circumference;
      if (typeof anyEl.height === "number") return anyEl.height;
      return 0;
    });
    return {
      type,
      labels: (chart.data.labels ?? []).map((l) => String(l)),
      data: (ds.data as number[]).map((n) => Number(n)),
      elementCount: elements.length,
      elementSizes,
      destroyed: false,
    };
  }

  private exposeHooks(): void {
    const w = window as unknown as Record<string, unknown>;
    w.__TESLAUSB_ANALYTICS__ = this;
    w.__TESLAUSB_ANALYTICS_HOOKS__ = {
      build: this.buildId,
      isRendered: () => this.rendered,
      events: () => this.snapshot(this.eventsChart, "doughnut"),
      trips: () => this.snapshot(this.tripsChart, "bar"),
    };
  }
}
