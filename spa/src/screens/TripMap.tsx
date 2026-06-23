import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Icon } from "../components/Icon";
import { api, ApiError } from "../api/client";
import type { Clip, DaySummary, EventItem, Trip, TripDetail } from "../api/types";
import {
  type ClockPref,
  TripMapController,
  type MapEvent,
  type MapFilters,
  type MapTrip,
} from "../map/controller";
import { activeSpeedBuckets, type SpeedUnit } from "../map/speed";

const METERS_PER_MILE = 1609.344;
const KM_PER_MILE = 1.609344;

type PanelTab = "events" | "trips" | "clips";
const PANEL_PAGE_SIZE = 25;

interface PanelTabState<T> {
  items: T[] | null;
  nextCursor: string | null;
  endReached: boolean;
  loading: boolean;
  error: boolean;
}

interface PanelState {
  events: PanelTabState<EventItem>;
  trips: PanelTabState<Trip>;
  clips: PanelTabState<Clip>;
}

function newPanelTabState<T>(): PanelTabState<T> {
  return {
    items: null,
    nextCursor: null,
    endReached: false,
    loading: false,
    error: false,
  };
}

function appendUniqueById<T extends { id: number }>(existing: T[], incoming: T[]): T[] {
  const seen = new Set(existing.map((item) => item.id));
  const merged = [...existing];
  for (const item of incoming) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    merged.push(item);
  }
  return merged;
}

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

function fmtClock(epochSec: number, clock: ClockPref): string {
  try {
    const options: Intl.DateTimeFormatOptions = {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    };
    if (clock === "utc") options.timeZone = "UTC";
    return new Date(epochSec * 1000).toLocaleString(undefined, options);
  } catch {
    return "—";
  }
}

function humanizeType(type: string): string {
  return type
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
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
 *  - panel    → `/api/events`, `/api/trips/page`, `/api/clips` (global cursor pages).
 *
 * The display-preference toggles (mph/kmh + local/UTC clock) are small SPA
 * functional additions (the legacy app was server-driven with no UI control) to
 * satisfy the parity gate.
 */
export function TripMap() {
  const mapRef = useRef<HTMLDivElement>(null);
  const ctrlRef = useRef<TripMapController | null>(null);
  const seqRef = useRef(0);

  const [days, setDays] = useState<DaySummary[] | null>(null);
  const [dayIndex, setDayIndex] = useState(0);
  const [unit, setUnit] = useState<SpeedUnit>("mph");
  const [clock, setClock] = useState<ClockPref>("local");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [prefError, setPrefError] = useState<string | null>(null);
  const [mapTrips, setMapTrips] = useState<MapTrip[]>([]);
  const [mapEvents, setMapEvents] = useState<MapEvent[]>([]);

  const [legendVisible, setLegendVisible] = useState(false);
  const [filtersVisible, setFiltersVisible] = useState(false);
  const [displayVisible, setDisplayVisible] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelTab, setPanelTab] = useState<PanelTab>("events");
  const [panelState, setPanelState] = useState<PanelState>({
    events: newPanelTabState<EventItem>(),
    trips: newPanelTabState<Trip>(),
    clips: newPanelTabState<Clip>(),
  });
  const panelStateRef = useRef(panelState);
  const panelListRef = useRef<HTMLDivElement>(null);
  const panelSentinelRef = useRef<HTMLDivElement | null>(null);
  const activePanelTabRef = useRef<PanelTab>("events");
  const panelRequestSeqRef = useRef<Record<PanelTab, number>>({
    events: 0,
    trips: 0,
    clips: 0,
  });
  const panelAbortRef = useRef<Record<PanelTab, AbortController | null>>({
    events: null,
    trips: null,
    clips: null,
  });
  const panelInFlightRef = useRef<Record<PanelTab, boolean>>({
    events: false,
    trips: false,
    clips: false,
  });

  const currentDay = days && days.length ? days[dayIndex] : null;
  const presentEventTypes = useMemo(
    () => Array.from(new Set(mapEvents.map((ev) => ev.type))).sort(),
    [mapEvents],
  );
  const maxDistanceMi = useMemo(
    () =>
      mapTrips.reduce((max, trip) => {
        const distance = Number.isFinite(trip.distanceMi) ? trip.distanceMi : 0;
        return Math.max(max, distance);
      }, 0),
    [mapTrips],
  );
  const [filters, setFilters] = useState<MapFilters>({
    enabledTypes: new Set<string>(),
    minSeverity: 0,
    minDistanceMi: 0,
    limitToView: false,
  });

  useEffect(() => {
    setFilters((prev) => ({
      ...prev,
      enabledTypes: new Set(presentEventTypes),
    }));
  }, [presentEventTypes.join("|")]);

  useEffect(() => {
    setFilters((prev) => ({
      ...prev,
      minDistanceMi: Math.min(prev.minDistanceMi, maxDistanceMi),
    }));
  }, [maxDistanceMi]);

  const maxDistanceDisplay = useMemo(() => {
    const displayMax =
      unit === "kph" ? maxDistanceMi * KM_PER_MILE : maxDistanceMi;
    return Math.max(0, Math.ceil(displayMax));
  }, [maxDistanceMi, unit]);
  const minDistanceDisplay = useMemo(
    () =>
      unit === "kph"
        ? filters.minDistanceMi * KM_PER_MILE
        : filters.minDistanceMi,
    [filters.minDistanceMi, unit],
  );

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
        const clockPref = settings.find((p) => p.key === "clock")?.value;
        setUnit(initialUnit);
        setClock(clockPref === "utc" ? "utc" : "local");
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

  // ── Load the selected day whenever it changes. ──
  useEffect(() => {
    if (!currentDay) return;
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
              severity: ev.severity ?? null,
              tripId: ev.trip_id ?? null,
              lat: ev.lat,
              lon: ev.lon,
              description: ev.description ?? "",
              t: ev.t,
              clipId: ev.clip_id ?? null,
            });
          }
        }
        const enabledTypes = new Set(mapEvents.map((ev) => ev.type));
        setFilters((prev) => ({ ...prev, enabledTypes }));
        setMapTrips(mapTrips);
        setMapEvents(mapEvents);
      } catch (err) {
        if (ac.signal.aborted || seq !== seqRef.current) return;
        setError(errMessage(err));
      } finally {
        if (seq === seqRef.current) setLoading(false);
      }
    })();

    return () => ac.abort();
  }, [currentDay?.day]);

  useEffect(() => {
    const ctrl = ctrlRef.current;
    if (!ctrl) return;
    ctrl.render({ trips: mapTrips, events: mapEvents, unit, clock, filters });
  }, [mapTrips, mapEvents, unit, clock, filters]);

  useEffect(() => {
    panelStateRef.current = panelState;
  }, [panelState]);

  const setActiveSentinel = useCallback((node: HTMLDivElement | null) => {
    panelSentinelRef.current = node;
  }, []);

  const abortTabRequest = useCallback((tab: PanelTab) => {
    const controller = panelAbortRef.current[tab];
    panelInFlightRef.current[tab] = false;
    if (!controller) return;
    controller.abort();
    panelAbortRef.current[tab] = null;
    panelRequestSeqRef.current[tab] += 1;
    setPanelState((prev) => ({
      ...prev,
      [tab]: { ...prev[tab], loading: false },
    }));
  }, []);

  const loadPanelPage = useCallback(
    async (tab: PanelTab, initial: boolean) => {
      if (panelInFlightRef.current[tab]) return;
      const state = panelStateRef.current[tab];
      if (state.loading) return;
      if (!initial && (state.items === null || state.nextCursor === null)) {
        return;
      }

      const seq = panelRequestSeqRef.current[tab] + 1;
      panelRequestSeqRef.current[tab] = seq;
      const controller = new AbortController();
      panelAbortRef.current[tab] = controller;
      panelInFlightRef.current[tab] = true;
      const cursor = initial ? undefined : state.nextCursor ?? undefined;
      setPanelState((prev) => ({
        ...prev,
        [tab]: { ...prev[tab], loading: true, error: false },
      }));

      try {
        if (tab === "events") {
          const page = await api.events(
            { cursor, limit: PANEL_PAGE_SIZE },
            controller.signal,
          );
          if (controller.signal.aborted || panelRequestSeqRef.current[tab] !== seq) {
            return;
          }
          setPanelState((prev) => {
            const prevTab = prev.events;
            const merged =
              initial || prevTab.items === null
                ? page.items
                : appendUniqueById(prevTab.items, page.items);
            return {
              ...prev,
              events: {
                items: merged,
                nextCursor: page.next_cursor,
                endReached: page.next_cursor === null,
                loading: false,
                error: false,
              },
            };
          });
          return;
        }
        if (tab === "trips") {
          const page = await api.tripsPage(
            { cursor, limit: PANEL_PAGE_SIZE },
            controller.signal,
          );
          if (controller.signal.aborted || panelRequestSeqRef.current[tab] !== seq) {
            return;
          }
          setPanelState((prev) => {
            const prevTab = prev.trips;
            const merged =
              initial || prevTab.items === null
                ? page.items
                : appendUniqueById(prevTab.items, page.items);
            return {
              ...prev,
              trips: {
                items: merged,
                nextCursor: page.next_cursor,
                endReached: page.next_cursor === null,
                loading: false,
                error: false,
              },
            };
          });
          return;
        }
        const page = await api.clips(
          { cursor, limit: PANEL_PAGE_SIZE },
          controller.signal,
        );
        if (controller.signal.aborted || panelRequestSeqRef.current[tab] !== seq) {
          return;
        }
        setPanelState((prev) => {
          const prevTab = prev.clips;
          const merged =
            initial || prevTab.items === null
              ? page.items
              : appendUniqueById(prevTab.items, page.items);
          return {
            ...prev,
            clips: {
              items: merged,
              nextCursor: page.next_cursor,
              endReached: page.next_cursor === null,
              loading: false,
              error: false,
            },
          };
        });
      } catch {
        if (controller.signal.aborted || panelRequestSeqRef.current[tab] !== seq) {
          return;
        }
        setPanelState((prev) => ({
          ...prev,
          [tab]: { ...prev[tab], loading: false, error: true },
        }));
      } finally {
        if (panelAbortRef.current[tab] === controller) {
          panelInFlightRef.current[tab] = false;
          panelAbortRef.current[tab] = null;
        }
      }
    },
    [],
  );

  const retryPanelTab = useCallback((tab: PanelTab) => {
    setPanelState((prev) => ({
      ...prev,
      [tab]: { ...prev[tab], error: false },
    }));
    const initial = panelStateRef.current[tab].items === null;
    void loadPanelPage(tab, initial);
  }, [loadPanelPage]);

  useEffect(() => {
    const previousTab = activePanelTabRef.current;
    if (previousTab !== panelTab) {
      abortTabRequest(previousTab);
      activePanelTabRef.current = panelTab;
    }
  }, [panelTab, abortTabRequest]);

  useEffect(() => {
    if (!panelOpen) {
      abortTabRequest("events");
      abortTabRequest("trips");
      abortTabRequest("clips");
      return;
    }
    if (panelStateRef.current[panelTab].items === null) {
      void loadPanelPage(panelTab, true);
    }
  }, [panelOpen, panelTab, abortTabRequest, loadPanelPage]);

  useEffect(() => {
    if (!panelOpen) return;
    const root = panelListRef.current;
    const sentinel = panelSentinelRef.current;
    const active = panelState[panelTab];
    if (!root || !sentinel || active.loading || active.error || active.nextCursor === null) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        void loadPanelPage(panelTab, false);
      },
      { root, rootMargin: "0px 0px 120px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [
    panelOpen,
    panelTab,
    panelState,
    loadPanelPage,
  ]);

  useEffect(
    () => () => {
      abortTabRequest("events");
      abortTabRequest("trips");
      abortTabRequest("clips");
    },
    [abortTabRequest],
  );

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

  // Per-key persisted-preference writer. The UI updates optimistically; this then
  // PUTs the value to /api/settings with three robustness guards (mirroring this
  // file's seqRef pattern for stale async results):
  //   • serialize per key so concurrent PUTs land in click order — the server
  //     converges to the user's latest choice even when a slow Pi reorders them;
  //   • track the last server-confirmed value per key (updated only on a
  //     successful PUT), so a failed write reverts the UI to what the server
  //     actually holds — never to an optimistic value that itself never persisted;
  //   • only the most-recently-enqueued write for a key may revert / surface an
  //     error, so a superseded failure can't clobber a newer value or show a
  //     stale toast.
  const prefWriteRef = useRef<
    Record<string, { seq: number; tail: Promise<unknown>; confirmed: string }>
  >({});

  const persistPref = (
    key: string,
    value: string,
    confirmedSeed: string,
    applyValue: (v: string) => void,
    errMsg: string,
  ) => {
    setPrefError(null);
    const slot = (prefWriteRef.current[key] ??= {
      seq: 0,
      tail: Promise.resolve(),
      confirmed: confirmedSeed,
    });
    const seq = ++slot.seq;
    const send = async () => {
      await api.putSetting(key, value);
      slot.confirmed = value;
    };
    slot.tail = slot.tail.then(send, send).then(
      () => undefined,
      () => {
        if (seq !== slot.seq) return;
        applyValue(slot.confirmed);
        setPrefError(errMsg);
      },
    );
  };

  const onToggleUnit = (next: SpeedUnit) => {
    if (next === unit) return;
    const prev = unit;
    setUnit(next);
    persistPref(
      "speed_unit",
      next,
      prev,
      (v) => setUnit(v as SpeedUnit),
      "Couldn't save speed unit. Keeping previous value.",
    );
  };

  const onToggleClock = (next: ClockPref) => {
    if (next === clock) return;
    const prev = clock;
    setClock(next);
    persistPref(
      "clock",
      next,
      prev,
      (v) => setClock(v as ClockPref),
      "Couldn't save clock preference. Keeping previous value.",
    );
  };

  const onToggleType = (type: string) => {
    setFilters((prev) => {
      const enabledTypes = new Set(prev.enabledTypes);
      if (enabledTypes.has(type)) enabledTypes.delete(type);
      else enabledTypes.add(type);
      return { ...prev, enabledTypes };
    });
  };

  const onMinDistanceChange = (rawValue: string) => {
    const parsed = Number(rawValue);
    const displayValue = Number.isFinite(parsed) ? parsed : 0;
    const thresholdMi =
      unit === "kph" ? displayValue / KM_PER_MILE : displayValue;
    setFilters((prev) => ({
      ...prev,
      minDistanceMi: Math.max(0, Math.min(thresholdMi, maxDistanceMi)),
    }));
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
      >
        {presentEventTypes.map((type) => {
          const enabled = filters.enabledTypes.has(type);
          return (
            <button
              type="button"
              class={`event-filter-pill${enabled ? " active" : ""}`}
              data-testid={`filter-type-${type}`}
              aria-pressed={enabled}
              onClick={() => onToggleType(type)}
            >
              <span class={`filter-pill-dot st-dot ${eventDotClass(type)}`} />
              <span>{humanizeType(type)}</span>
            </button>
          );
        })}
      </div>

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

      <div
        class={`filter-panel${filtersVisible ? " visible" : ""}`}
        id="filterPanel"
        aria-hidden={filtersVisible ? "false" : "true"}
      >
        <div class="filter-panel-title">Filters</div>
        <div class="filter-section">
          <div class="filter-label">Severity</div>
          <div class="filter-segmented" role="group" aria-label="Minimum severity">
            <button
              type="button"
              data-testid="filter-sev-all"
              class={filters.minSeverity === 0 ? "active" : ""}
              aria-pressed={filters.minSeverity === 0}
              onClick={() => setFilters((prev) => ({ ...prev, minSeverity: 0 }))}
            >
              All
            </button>
            <button
              type="button"
              data-testid="filter-sev-info"
              class={filters.minSeverity === 1 ? "active" : ""}
              aria-pressed={filters.minSeverity === 1}
              onClick={() => setFilters((prev) => ({ ...prev, minSeverity: 1 }))}
            >
              Info+
            </button>
            <button
              type="button"
              data-testid="filter-sev-warning"
              class={filters.minSeverity === 2 ? "active" : ""}
              aria-pressed={filters.minSeverity === 2}
              onClick={() => setFilters((prev) => ({ ...prev, minSeverity: 2 }))}
            >
              Warning+
            </button>
            <button
              type="button"
              data-testid="filter-sev-critical"
              class={filters.minSeverity === 3 ? "active" : ""}
              aria-pressed={filters.minSeverity === 3}
              onClick={() => setFilters((prev) => ({ ...prev, minSeverity: 3 }))}
            >
              Critical
            </button>
          </div>
        </div>
        <div class="filter-section">
          <label class="filter-label" htmlFor="filterMinDistance">
            Minimum trip distance
          </label>
          <input
            type="range"
            id="filterMinDistance"
            min="0"
            max={String(maxDistanceDisplay)}
            step="0.1"
            value={String(Math.min(minDistanceDisplay, maxDistanceDisplay))}
            onInput={(e) =>
              onMinDistanceChange((e.currentTarget as HTMLInputElement).value)
            }
          />
          <div id="filterMinDistanceValue" class="filter-value">
            {`${minDistanceDisplay.toFixed(1)} ${unit === "kph" ? "km" : "mi"}`}
          </div>
        </div>
        <div class="filter-section">
          <button
            type="button"
            id="filterLimitView"
            class={`filter-switch${filters.limitToView ? " active" : ""}`}
            role="switch"
            aria-checked={filters.limitToView ? "true" : "false"}
            onClick={() =>
              setFilters((prev) => ({ ...prev, limitToView: !prev.limitToView }))
            }
          >
            Limit to map view
          </button>
        </div>
      </div>

      <div
        class={`display-panel${displayVisible ? " visible" : ""}`}
        id="displayPanel"
        aria-hidden={displayVisible ? "false" : "true"}
      >
        <div class="filter-panel-title">Display</div>
        <div class="filter-section">
          <div class="filter-label">Clock</div>
          <span class="speed-unit-toggle" role="group" aria-label="Clock">
            <button
              type="button"
              class={`speed-unit-btn${clock === "local" ? " active" : ""}`}
              id="clockLocal"
              aria-pressed={clock === "local"}
              onClick={() => onToggleClock("local")}
            >
              Local
            </button>
            <button
              type="button"
              class={`speed-unit-btn${clock === "utc" ? " active" : ""}`}
              id="clockUtc"
              aria-pressed={clock === "utc"}
              onClick={() => onToggleClock("utc")}
            >
              UTC
            </button>
          </span>
        </div>
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
          class={`map-fab${filtersVisible ? " active" : ""}`}
          id="btnFilters"
          onClick={() => setFiltersVisible((v) => !v)}
          aria-label="Filters"
          title="Filters"
        >
          <Icon name="filter" />
        </button>
        <button
          class={`map-fab${displayVisible ? " active" : ""}`}
          id="btnDisplayPrefs"
          onClick={() => setDisplayVisible((v) => !v)}
          aria-label="Display preferences"
          title="Display"
        >
          <Icon name="settings" />
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
      {prefError && (
        <div class="map-pref-error" role="status" aria-live="polite">
          {prefError}
        </div>
      )}

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
        <div class="video-panel-list" id="vpList" ref={panelListRef}>
          {panelTab === "events" && (
            <EventsTab
              events={panelState.events.items}
              clock={clock}
              loading={panelState.events.loading}
              endReached={panelState.events.endReached}
              error={panelState.events.error}
              sentinelRef={setActiveSentinel}
              onRetry={() => retryPanelTab("events")}
            />
          )}
          {panelTab === "trips" && (
            <TripsTab
              trips={panelState.trips.items}
              clock={clock}
              loading={panelState.trips.loading}
              endReached={panelState.trips.endReached}
              error={panelState.trips.error}
              sentinelRef={setActiveSentinel}
              onRetry={() => retryPanelTab("trips")}
            />
          )}
          {panelTab === "clips" && (
            <ClipsTab
              clips={panelState.clips.items}
              clock={clock}
              loading={panelState.clips.loading}
              endReached={panelState.clips.endReached}
              error={panelState.clips.error}
              sentinelRef={setActiveSentinel}
              onRetry={() => retryPanelTab("clips")}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function EventsTab({
  events,
  clock,
  loading,
  endReached,
  error,
  sentinelRef,
  onRetry,
}: {
  events: EventItem[] | null;
  clock: ClockPref;
  loading: boolean;
  endReached: boolean;
  error: boolean;
  sentinelRef: (node: HTMLDivElement | null) => void;
  onRetry: () => void;
}) {
  if (events === null) {
    if (error && !loading)
      return (
        <div class="vp-error" data-testid="vp-error-events">
          <span>Couldn't load events.</span>
          <button
            type="button"
            class="vp-retry"
            data-testid="vp-retry-events"
            onClick={onRetry}
          >
            Retry
          </button>
        </div>
      );
    return <div class="vp-loading">Loading events…</div>;
  }
  if (events.length === 0) {
    if (loading) return <div class="vp-loading">Loading events…</div>;
    return <div class="vp-empty">No events</div>;
  }
  return (
    <div class="sentry-timeline" data-testid="vp-events">
      <div class="st-summary">
        <strong>
          {events.length} Event{events.length !== 1 ? "s" : ""}
        </strong>
      </div>
      {events.map((ev) => {
        const inner = (
          <>
            <div class="st-type">{ev.type.replace(/_/g, " ")}</div>
            <div class="st-date">{fmtClock(ev.t, clock)}</div>
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
          </>
        );
        return (
          <div class="st-event" key={ev.id}>
            <span class={`st-dot ${eventDotClass(ev.type)}`} />
            {ev.clip_id != null ? (
              <a
                class="st-card st-card-link"
                href={`/events?event=${ev.id}`}
                data-testid={`vp-event-link-${ev.id}`}
                aria-label={`Watch ${ev.type.replace(/_/g, " ")} event`}
              >
                {inner}
              </a>
            ) : (
              <div class="st-card">{inner}</div>
            )}
          </div>
        );
      })}
      {loading && <div class="vp-loading">Loading…</div>}
      {error && !loading && (
        <div class="vp-error" data-testid="vp-error-events">
          <span>Couldn't load more.</span>
          <button
            type="button"
            class="vp-retry"
            data-testid="vp-retry-events"
            onClick={onRetry}
          >
            Retry
          </button>
        </div>
      )}
      {!loading && !endReached && !error && (
        <div
          class="vp-sentinel"
          data-testid="vp-sentinel-events"
          ref={sentinelRef}
        />
      )}
      {endReached && <div class="vp-end" data-testid="vp-end-events">No more</div>}
    </div>
  );
}

function TripsTab({
  trips,
  clock,
  loading,
  endReached,
  error,
  sentinelRef,
  onRetry,
}: {
  trips: Trip[] | null;
  clock: ClockPref;
  loading: boolean;
  endReached: boolean;
  error: boolean;
  sentinelRef: (node: HTMLDivElement | null) => void;
  onRetry: () => void;
}) {
  if (trips === null) {
    if (error && !loading)
      return (
        <div class="vp-error" data-testid="vp-error-trips">
          <span>Couldn't load trips.</span>
          <button
            type="button"
            class="vp-retry"
            data-testid="vp-retry-trips"
            onClick={onRetry}
          >
            Retry
          </button>
        </div>
      );
    return <div class="vp-loading">Loading trips…</div>;
  }
  if (trips.length === 0) {
    if (loading) return <div class="vp-loading">Loading trips…</div>;
    return <div class="vp-empty">No trips this day</div>;
  }
  return (
    <div data-testid="vp-trips">
      {trips.map((t) => (
        <div class="vp-clip" key={t.id}>
          <div class="vp-clip-info">
            <div class="vp-clip-date">Trip #{t.id}</div>
            <div class="vp-clip-meta">
              {fmtClock(t.started_at, clock)} · {t.point_count} pts
            </div>
            <div class="vp-clip-reason">
              {((t.distance_m ?? 0) / METERS_PER_MILE).toFixed(1)} mi
            </div>
          </div>
        </div>
      ))}
      {loading && <div class="vp-loading">Loading…</div>}
      {error && !loading && (
        <div class="vp-error" data-testid="vp-error-trips">
          <span>Couldn't load more.</span>
          <button
            type="button"
            class="vp-retry"
            data-testid="vp-retry-trips"
            onClick={onRetry}
          >
            Retry
          </button>
        </div>
      )}
      {!loading && !endReached && !error && (
        <div
          class="vp-sentinel"
          data-testid="vp-sentinel-trips"
          ref={sentinelRef}
        />
      )}
      {endReached && <div class="vp-end" data-testid="vp-end-trips">No more</div>}
    </div>
  );
}

function ClipsTab({
  clips,
  clock,
  loading,
  endReached,
  error,
  sentinelRef,
  onRetry,
}: {
  clips: Clip[] | null;
  clock: ClockPref;
  loading: boolean;
  endReached: boolean;
  error: boolean;
  sentinelRef: (node: HTMLDivElement | null) => void;
  onRetry: () => void;
}) {
  if (clips === null) {
    if (error && !loading)
      return (
        <div class="vp-error" data-testid="vp-error-clips">
          <span>Couldn't load clips.</span>
          <button
            type="button"
            class="vp-retry"
            data-testid="vp-retry-clips"
            onClick={onRetry}
          >
            Retry
          </button>
        </div>
      );
    return <div class="vp-loading">Loading clips…</div>;
  }
  if (clips.length === 0) {
    if (loading) return <div class="vp-loading">Loading clips…</div>;
    return <div class="vp-empty">No clips</div>;
  }
  return (
    <div data-testid="vp-clips">
      {clips.map((c) => (
        <a
          class="vp-clip vp-clip-link"
          key={c.id}
          href={`/events?clip=${c.id}`}
          data-testid={`vp-clip-link-${c.id}`}
          aria-label={`Open clip ${fmtClock(c.started_at, clock)}`}
        >
          <div class="vp-clip-info">
            <div class="vp-clip-date">{fmtClock(c.started_at, clock)}</div>
            <div class="vp-clip-meta">
              {c.angles.length} cam · {c.folder_class}
            </div>
            {c.is_sentry && <div class="vp-clip-reason">sentry</div>}
          </div>
        </a>
      ))}
      {loading && <div class="vp-loading">Loading…</div>}
      {error && !loading && (
        <div class="vp-error" data-testid="vp-error-clips">
          <span>Couldn't load more.</span>
          <button
            type="button"
            class="vp-retry"
            data-testid="vp-retry-clips"
            onClick={onRetry}
          >
            Retry
          </button>
        </div>
      )}
      {!loading && !endReached && !error && (
        <div
          class="vp-sentinel"
          data-testid="vp-sentinel-clips"
          ref={sentinelRef}
        />
      )}
      {endReached && <div class="vp-end" data-testid="vp-end-clips">No more</div>}
    </div>
  );
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : (err as Error).message;
}
