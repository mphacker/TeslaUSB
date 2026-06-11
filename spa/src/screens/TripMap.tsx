import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Icon } from "../components/Icon";
import { api, ApiError } from "../api/client";
import type { Clip, DaySummary, EventItem, Trip, TripDetail } from "../api/types";
import { TripMapController, type MapEvent, type MapTrip } from "../map/controller";
import { activeSpeedBuckets, type SpeedUnit } from "../map/speed";

const METERS_PER_MILE = 1609.344;

type PanelTab = "events" | "trips" | "clips";

/** Map a webd event type onto the legacy sentry-timeline dot class. */
function eventDotClass(type: string): string {
  switch (type) {
    case "sentry":
      return "sentry";
    case "saved":
      return "saved";
    case "harsh_braking":
    case "emergency_braking":
      return "driving-critical";
    case "hard_acceleration":
    case "sharp_turn":
    case "speed_limit_exceeded":
    case "honk":
      return "driving";
    case "autopilot_engaged":
    case "autopilot_disengaged":
      return "fsd";
    default:
      return "trip";
  }
}

/** Map the indexd-derived severity ordinal (1=info, 2=warning, 3=critical) to
 *  its label. `severity` is NOT a speed — it has no units. */
function severityLabel(severity: number | null): string | null {
  switch (severity) {
    case 1:
      return "info";
    case 2:
      return "warning";
    case 3:
      return "critical";
    default:
      return null;
  }
}

function fmtClock(epochSec: number): string {
  try {
    return new Date(epochSec * 1000).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return "—";
  }
}

/** Build the per-day renderable model from webd trip detail + bubble events. */
function toMapTrip(trip: Trip, detail: TripDetail | null): MapTrip {
  const points = detail?.points ?? [];
  const waypoints = points
    .filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon))
    .map((p) => ({ lat: p.lat, lon: p.lon, speed: p.speed ?? 0 }));
  const distanceMi = (trip.distance_m ?? 0) / METERS_PER_MILE;
  const durationMin = Math.max(0, Math.round((trip.ended_at - trip.started_at) / 60));
  return {
    id: trip.id,
    startTime: trip.started_at,
    distanceMi,
    durationMin,
    waypoints,
    polyline: trip.polyline ?? [],
    startCoord: null,
    endCoord: null,
  };
}

/**
 * The trip-map screen (route `/`, Shell active "map") — visual + structural +
 * functional parity with the legacy Flask `mapping.html`. The DOM below mirrors
 * that template element-for-element (`.map-container` and its floating overlays)
 * so the carried `mapping.css` lands exactly; the Leaflet map itself is driven
 * imperatively by {@link TripMapController} via a ref/effect (Leaflet is not a
 * Preact library), which is the clean pattern future imperative-lib screens
 * (Chart.js analytics, dashcam-MP4 HUD) mirror.
 *
 * Data comes only from webd's read-only catalog API:
 *  - day nav  → `/api/days`
 *  - routes   → `/api/trips?day=` + per-trip `/api/trips/:id` (points + speed),
 *               falling back to the trip's pre-decoded `polyline` segments.
 *  - bubbles  → bounded per-trip `/api/events?trip=<id>` (on-route events).
 *  - panel    → `/api/events` (all, incl. trip-less), `/api/trips`, `/api/clips`.
 *
 * The mph/kmh toggle is a small SPA functional addition (the legacy app was
 * server-driven with no UI control) to satisfy the spa.md §3 parity gate.
 */
export function TripMap() {
  const mapRef = useRef<HTMLDivElement>(null);
  const ctrlRef = useRef<TripMapController | null>(null);
  const seqRef = useRef(0);

  const [days, setDays] = useState<DaySummary[] | null>(null);
  const [dayIndex, setDayIndex] = useState(0);
  const [unit, setUnit] = useState<SpeedUnit>("mph");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [legendVisible, setLegendVisible] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelTab, setPanelTab] = useState<PanelTab>("events");
  const [panelEvents, setPanelEvents] = useState<EventItem[] | null>(null);
  const [panelTrips, setPanelTrips] = useState<Trip[] | null>(null);
  const [panelClips, setPanelClips] = useState<Clip[] | null>(null);

  const currentDay = days && days.length ? days[dayIndex] : null;

  // ── Mount: create the Leaflet controller, add the mapping-active body class,
  //    seed the display unit from prefs, and load the day list. ──
  useEffect(() => {
    if (!mapRef.current) return;
    const ctrl = new TripMapController(mapRef.current);
    ctrlRef.current = ctrl;
    // map.css carries three global overrides (full-height, no page scroll); we
    // scope them to .mapping-active so they apply ONLY while the map is mounted.
    document.documentElement.classList.add("mapping-active");
    document.body.classList.add("mapping-active");
    // Leaflet must recompute size once its container has its final layout.
    requestAnimationFrame(() => ctrl.invalidate());

    const boot = new AbortController();
    (async () => {
      try {
        const [dayList, settings] = await Promise.all([
          api.days(boot.signal),
          api.settings(boot.signal),
        ]);
        const pref = settings.find((p) => p.key === "speed_unit")?.value ?? "";
        const initialUnit: SpeedUnit = /kph|km/i.test(pref) ? "kph" : "mph";
        setUnit(initialUnit);
        setDays(dayList);
        setDayIndex(0);
      } catch (err) {
        if (boot.signal.aborted) return;
        setError(errMessage(err));
      }
    })();

    return () => {
      boot.abort();
      document.documentElement.classList.remove("mapping-active");
      document.body.classList.remove("mapping-active");
      ctrl.destroy();
      ctrlRef.current = null;
    };
  }, []);

  // ── Load + render the selected day whenever it (or the unit) changes. ──
  useEffect(() => {
    const ctrl = ctrlRef.current;
    if (!ctrl || !currentDay) return;
    const seq = ++seqRef.current;
    const ac = new AbortController();
    setLoading(true);

    (async () => {
      try {
        const trips = await api.trips(currentDay.day, ac.signal);
        // Per-trip detail (points + speed) and bounded on-route bubble events.
        const details = await Promise.all(
          trips.map((t) =>
            api.trip(t.id, ac.signal).catch(() => null as TripDetail | null),
          ),
        );
        const eventPages = await Promise.all(
          trips.map((t) =>
            api
              .events({ trip: t.id, limit: 500 }, ac.signal)
              .then((p) => p.items)
              .catch(() => [] as EventItem[]),
          ),
        );
        if (seq !== seqRef.current) return;

        const mapTrips: MapTrip[] = trips.map((t, i) => toMapTrip(t, details[i]));
        const mapEvents: MapEvent[] = [];
        for (const items of eventPages) {
          for (const ev of items) {
            if (ev.lat == null || ev.lon == null) continue;
            mapEvents.push({
              id: ev.id,
              type: ev.type,
              lat: ev.lat,
              lon: ev.lon,
              description: ev.description ?? "",
              t: ev.t,
            });
          }
        }
        ctrl.render({ trips: mapTrips, events: mapEvents, unit });
      } catch (err) {
        if (ac.signal.aborted || seq !== seqRef.current) return;
        setError(errMessage(err));
      } finally {
        if (seq === seqRef.current) setLoading(false);
      }
    })();

    return () => ac.abort();
  }, [currentDay?.day, unit]);

  // ── Lazy-load the video-panel data for the active tab when opened. ──
  useEffect(() => {
    if (!panelOpen) return;
    const ac = new AbortController();
    (async () => {
      try {
        if (panelTab === "events" && panelEvents === null) {
          const page = await api.events({ limit: 100 }, ac.signal);
          setPanelEvents(page.items);
        } else if (panelTab === "trips") {
          if (currentDay) {
            const t = await api.trips(currentDay.day, ac.signal);
            setPanelTrips(t);
          } else {
            setPanelTrips([]);
          }
        } else if (panelTab === "clips" && panelClips === null) {
          const page = await api.clips({ limit: 100 }, ac.signal);
          setPanelClips(page.items);
        }
      } catch {
        /* panel data is best-effort; leave the loading state for the user */
      }
    })();
    return () => ac.abort();
  }, [panelOpen, panelTab, currentDay?.day]);

  const buckets = useMemo(() => activeSpeedBuckets(unit), [unit]);

  const cycleDay = (delta: number) => {
    // Legacy semantics: -1 = older (toward higher DESC index), +1 = newer.
    if (!days || !days.length) return;
    setDayIndex((i) => {
      const next = i - delta;
      if (next < 0 || next >= days.length) return i;
      return next;
    });
  };

  const onToggleUnit = (next: SpeedUnit) => {
    if (next === unit) return;
    setUnit(next);
  };

  const dayStats = currentDay
    ? `${currentDay.trip_count} ${currentDay.trip_count === 1 ? "trip" : "trips"} \u00B7 ` +
      `${currentDay.event_count} ${currentDay.event_count === 1 ? "event" : "events"} \u00B7 ` +
      `${(currentDay.distance_m / METERS_PER_MILE).toFixed(1)} mi`
    : "\u2014";

  const dayLabel = currentDay
    ? new Date(`${currentDay.day}T00:00:00`).toLocaleDateString(undefined, {
        weekday: "short",
        month: "short",
        day: "numeric",
        year: "numeric",
      })
    : error
      ? "Unavailable"
      : "Loading\u2026";

  return (
    <div
      class={`map-container${loading ? " is-loading" : ""}`}
      data-screen="trip-map"
    >
      <div id="map" ref={mapRef} />

      <div
        class="map-loading-bar"
        id="mapLoadingBar"
        role="progressbar"
        aria-label="Loading map data"
        aria-busy={loading ? "true" : "false"}
        aria-hidden={loading ? "false" : "true"}
      >
        <div class="map-loading-bar-fill" />
      </div>

      <div
        class="event-filter-pills"
        id="eventFilterPills"
        role="toolbar"
        aria-label="Filter event markers"
      />

      <div class="trip-card" id="tripCard">
        <div class="trip-card-nav">
          <button
            class="trip-nav-btn"
            id="dayPrev"
            onClick={() => cycleDay(-1)}
            disabled={!days || dayIndex >= (days.length - 1)}
            aria-label="Older day"
          >
            <Icon name="chevron-left" />
          </button>
          <div class="trip-card-info">
            <div class="trip-card-date" id="dayCardDate">
              {dayLabel}
            </div>
            <div class="trip-card-stats" id="dayCardStats">
              {dayStats}
            </div>
          </div>
          <button
            class="trip-nav-btn"
            id="dayNext"
            onClick={() => cycleDay(1)}
            disabled={!days || dayIndex <= 0}
            aria-label="Newer day"
          >
            <Icon name="chevron-right" />
          </button>
        </div>
        <div class="trip-card-indexed" id="tripCardIndexed">
          <span id="tripIndexedCount">
            {days ? days.length : "\u2014"}
          </span>{" "}
          {days && days.length === 1 ? "day mapped" : "days mapped"}
        </div>
      </div>

      <div
        class={`speed-legend${legendVisible ? " visible" : ""}`}
        id="speedLegend"
        aria-hidden={legendVisible ? "false" : "true"}
      >
        <div class="speed-legend-title">
          Speed ({unit})
          <span class="speed-unit-toggle" role="group" aria-label="Speed unit">
            <button
              type="button"
              class={`speed-unit-btn${unit === "mph" ? " active" : ""}`}
              id="speedUnitMph"
              aria-pressed={unit === "mph"}
              onClick={() => onToggleUnit("mph")}
            >
              mph
            </button>
            <button
              type="button"
              class={`speed-unit-btn${unit === "kph" ? " active" : ""}`}
              id="speedUnitKph"
              aria-pressed={unit === "kph"}
              onClick={() => onToggleUnit("kph")}
            >
              kmh
            </button>
          </span>
        </div>
        {buckets.map((b, i) => (
          <div class="speed-legend-row" key={i}>
            <span class={`speed-legend-swatch speed-legend-swatch-${i}`} />
            <span>{b.label}</span>
          </div>
        ))}
      </div>

      <div class="map-fab-group" id="mapFabs">
        <button
          class={`map-fab${panelOpen ? " active" : ""}`}
          id="btnVideos"
          onClick={() => setPanelOpen((o) => !o)}
          aria-label="Video browser"
          title="Videos"
        >
          <Icon name="video" />
        </button>
        <button
          class={`map-fab${legendVisible ? " active" : ""}`}
          id="btnSpeedLegend"
          onClick={() => setLegendVisible((v) => !v)}
          aria-label="Speed color legend"
          title="Speed Legend"
        >
          <Icon name="zap" />
        </button>
      </div>

      <div class={`video-panel${panelOpen ? " open" : ""}`} id="videoPanel">
        <div class="video-panel-header">
          <div class="vp-tabs">
            <button
              class={`vp-tab${panelTab === "events" ? " active" : ""}`}
              id="vpTabSentry"
              onClick={() => setPanelTab("events")}
            >
              Events
            </button>
            <button
              class={`vp-tab${panelTab === "trips" ? " active" : ""}`}
              id="vpTabTrips"
              onClick={() => setPanelTab("trips")}
            >
              Trips
            </button>
            <button
              class={`vp-tab${panelTab === "clips" ? " active" : ""}`}
              id="vpTabClips"
              onClick={() => setPanelTab("clips")}
            >
              All Clips
            </button>
          </div>
          <button
            class="close-btn"
            onClick={() => setPanelOpen(false)}
            aria-label="Close video panel"
          >
            <Icon name="x" />
          </button>
        </div>
        <div class="video-panel-list" id="vpList">
          {panelTab === "events" && (
            <EventsTab events={panelEvents} />
          )}
          {panelTab === "trips" && <TripsTab trips={panelTrips} />}
          {panelTab === "clips" && <ClipsTab clips={panelClips} />}
        </div>
      </div>
    </div>
  );
}

function EventsTab({
  events,
}: {
  events: EventItem[] | null;
}) {
  if (events === null) return <div class="vp-loading">Loading events…</div>;
  if (events.length === 0) return <div class="vp-empty">No events</div>;
  return (
    <div class="sentry-timeline" data-testid="vp-events">
      <div class="st-summary">
        <strong>
          {events.length} Event{events.length !== 1 ? "s" : ""}
        </strong>
      </div>
      {events.map((ev) => (
        <div class="st-event" key={ev.id}>
          <span class={`st-dot ${eventDotClass(ev.type)}`} />
          <div class="st-card">
            <div class="st-type">{ev.type.replace(/_/g, " ")}</div>
            <div class="st-date">{fmtClock(ev.t)}</div>
            <div class="st-meta">
              {ev.description ||
                (ev.trip_id != null ? `Trip #${ev.trip_id}` : "Standalone")}
              {ev.lat != null && ev.lon != null
                ? ` \u00B7 ${ev.lat.toFixed(4)}, ${ev.lon.toFixed(4)}`
                : ""}
              {severityLabel(ev.severity)
                ? ` \u00B7 ${severityLabel(ev.severity)}`
                : ""}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function TripsTab({ trips }: { trips: Trip[] | null }) {
  if (trips === null) return <div class="vp-loading">Loading trips…</div>;
  if (trips.length === 0) return <div class="vp-empty">No trips this day</div>;
  return (
    <div data-testid="vp-trips">
      {trips.map((t) => (
        <div class="vp-clip" key={t.id}>
          <div class="vp-clip-info">
            <div class="vp-clip-date">Trip #{t.id}</div>
            <div class="vp-clip-meta">
              {fmtClock(t.started_at)} · {t.point_count} pts
            </div>
            <div class="vp-clip-reason">
              {((t.distance_m ?? 0) / METERS_PER_MILE).toFixed(1)} mi
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function ClipsTab({ clips }: { clips: Clip[] | null }) {
  if (clips === null) return <div class="vp-loading">Loading clips…</div>;
  if (clips.length === 0) return <div class="vp-empty">No clips</div>;
  return (
    <div data-testid="vp-clips">
      {clips.map((c) => (
        <div class="vp-clip" key={c.id}>
          <div class="vp-clip-info">
            <div class="vp-clip-date">{fmtClock(c.started_at)}</div>
            <div class="vp-clip-meta">
              {c.angles.length} cam · {c.folder_class}
            </div>
            {c.is_sentry && <div class="vp-clip-reason">sentry</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : (err as Error).message;
}
