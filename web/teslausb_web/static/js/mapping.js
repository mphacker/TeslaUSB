import { createEventsPanel } from "./mapping/events_panel.js";
import { createIndexerPanel } from "./mapping/indexer_panel.js";
import { createMapRenderer } from "./mapping/map_renderer.js";
import { createSentryInspector } from "./mapping/sentry_inspector.js";

const page = document.getElementById("mapping-page");
const bootstrap = window.MAPPING_BOOTSTRAP || null;

if (!page || !bootstrap) {
    throw new Error("Mapping bootstrap is missing.");
}

const state = {
    days: [],
    currentDate: "",
    latestDate: "",
    showingAllRoutes: bootstrap.view.mode === "all",
    enabledTypes: new Set(),
    currentTrips: [],
    currentEvents: [],
    currentTripId: null,
    stats: null,
    drivingStats: null,
    charts: null,
};

function notify(message, tone = "info") {
    if (typeof window.showToast === "function") {
        window.showToast(message, tone);
        return;
    }
    window.alert(message);
}

async function fetchJson(url, options) {
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(payload.error || payload.message || `Request failed: ${response.status}`);
    }
    return payload;
}

function replaceTemplate(url, replacements) {
    let next = url;
    Object.entries(replacements).forEach(([key, value]) => {
        next = next.replace(key, String(value));
    });
    return next;
}

function formatDayLabel(dateText) {
    if (!dateText) {
        return "All routes";
    }
    const [year, month, day] = dateText.split("-").map((part) => Number.parseInt(part, 10));
    const date = new Date(year, (month || 1) - 1, day || 1);
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function filterSummaryRows(events) {
    const counts = new Map();
    events.forEach((event) => {
        const eventType = event.event_type || "unknown";
        counts.set(eventType, (counts.get(eventType) || 0) + 1);
    });
    return Array.from(counts.entries()).sort((left, right) => left[0].localeCompare(right[0])).map(([eventType, count]) => ({
        eventType,
        count,
        enabled: state.enabledTypes.has(eventType),
        iconName: eventType === "sentry" ? "shield-alert" : eventType === "saved" ? "bookmark" : eventType === "autopilot" ? "bot" : eventType === "speeding" ? "gauge" : "triangle-alert",
        accent: eventType === "sentry" ? "var(--msg-error-text)" : eventType === "saved" ? "var(--btn-info-bg)" : eventType === "autopilot" ? "var(--msg-success-text)" : eventType === "speeding" ? "var(--btn-danger-bg)" : "var(--text-secondary)",
    }));
}

async function loadTripRoute(tripId) {
    return fetchJson(replaceTemplate(bootstrap.api.trip_route_template, { __TRIP_ID__: tripId }));
}

async function loadTripTelemetry(tripId) {
    return fetchJson(
        replaceTemplate(bootstrap.api.trip_telemetry_template, { __TRIP_ID__: tripId })
    );
}

const mapRenderer = createMapRenderer({
    frame: document.getElementById("mappingMapFrame"),
    mapElement: document.getElementById("mappingMap"),
    filterPills: document.getElementById("mappingFilterPills"),
    modePill: document.getElementById("mappingModePill"),
    summaryText: document.getElementById("mappingMapSummaryText"),
    routeChip: document.getElementById("mappingRouteChip"),
    eventChip: document.getElementById("mappingEventChip"),
    spriteUrl: bootstrap.assets.sprite,
    leafletIconPath: bootstrap.assets.leaflet_icon_path,
    tileCacheUrl: bootstrap.assets.tile_cache_sw,
    onToggleEventType(eventType) {
        if (state.enabledTypes.has(eventType)) {
            state.enabledTypes.delete(eventType);
        } else {
            state.enabledTypes.add(eventType);
        }
        renderCurrentView();
    },
    onSelectEvent(event) {
        mapRenderer.focusEvent(event);
        sentryInspector.openEvent(event).catch((error) => notify(error.message, "warning"));
    },
    onSelectTrip(trip, options = {}) {
        if (options.fromAllRoutes && trip.date) {
            loadDay(trip.date, trip.trip_id || trip.id).catch((error) => notify(error.message, "warning"));
            return;
        }
        state.currentTripId = trip.trip_id || trip.id;
        mapRenderer.focusTrip(trip);
        eventsPanel.renderTrips(state.currentTrips, state.currentTripId);
    },
    onOpenClip({ trip, waypoint }) {
        const tripId = trip.trip_id || trip.id;
        Promise.all([loadTripRoute(tripId), loadTripTelemetry(tripId)])
            .then(([route, telemetry]) => sentryInspector.openTripClip({
                summary: `Trip ${tripId} · ${formatDayLabel(state.currentDate)}`,
                timestamp: waypoint.timestamp,
                videoPath: waypoint.video_path,
                waypoints: route.properties?.waypoints || [],
                telemetry: telemetry.telemetry || {},
            }))
            .catch((error) => notify(error.message, "warning"));
    },
});

const eventsPanel = createEventsPanel({
    dayList: document.getElementById("mappingDayList"),
    tripList: document.getElementById("mappingTripList"),
    filterList: document.getElementById("mappingFilterList"),
    dayCountChip: document.getElementById("mappingDayCountChip"),
    tripCountChip: document.getElementById("mappingTripCountChip"),
    daysEmpty: document.getElementById("mappingDaysEmpty"),
    tripsEmpty: document.getElementById("mappingTripsEmpty"),
    filtersEmpty: document.getElementById("mappingFiltersEmpty"),
    returnButton: document.getElementById("mappingReturnToDayButton"),
    statsGrid: document.getElementById("mappingStatsGrid"),
    typeChart: document.getElementById("mappingTypeChart"),
    severityChart: document.getElementById("mappingSeverityChart"),
    timelineChart: document.getElementById("mappingTimelineChart"),
    fsdChart: document.getElementById("mappingFsdChart"),
    spriteUrl: bootstrap.assets.sprite,
    onSelectDay(date) {
        loadDay(date).catch((error) => notify(error.message, "warning"));
    },
    onSelectTrip(trip) {
        state.currentTripId = trip.trip_id || trip.id;
        mapRenderer.focusTrip(trip);
        eventsPanel.renderTrips(state.currentTrips, state.currentTripId);
    },
    onToggleEventType(eventType) {
        if (state.enabledTypes.has(eventType)) {
            state.enabledTypes.delete(eventType);
        } else {
            state.enabledTypes.add(eventType);
        }
        renderCurrentView();
    },
    onReturnToLatestDay() {
        if (state.latestDate) {
            loadDay(state.latestDate).catch((error) => notify(error.message, "warning"));
        }
    },
});

const sentryEvents = [];
const sentryInspector = createSentryInspector({
    sentryList: document.getElementById("mappingSentryList"),
    sentryEmpty: document.getElementById("mappingSentryEmpty"),
    playerCard: document.getElementById("mappingPlayerCard"),
    playerTitle: document.getElementById("mappingPlayerTitle"),
    playerSummary: document.getElementById("mappingPlayerSummary"),
    playerVideo: document.getElementById("mappingPlayerVideo"),
    cameraRow: document.getElementById("mappingCameraRow"),
    prevClipButton: document.getElementById("mappingPrevClipButton"),
    nextClipButton: document.getElementById("mappingNextClipButton"),
    spriteUrl: bootstrap.assets.sprite,
    dashcamProtoUrl: bootstrap.assets.dashcam_proto,
    streamTemplate: bootstrap.view.video_stream_template,
    hud: {
        coords: document.getElementById("mappingHudCoords"),
        speed: document.getElementById("mappingHudSpeed"),
        gear: document.getElementById("mappingHudGear"),
        autopilot: document.getElementById("mappingHudAutopilot"),
        steering: document.getElementById("mappingHudSteering"),
        brake: document.getElementById("mappingHudBrake"),
        blinkers: document.getElementById("mappingHudBlinkers"),
        source: document.getElementById("mappingHudSource"),
    },
    api: bootstrap.api,
    fetchJson,
    loadTripRoute,
    loadTripTelemetry,
    onSelectEvent(event) {
        mapRenderer.focusEvent(event);
    },
    notify,
});

const indexerPanel = createIndexerPanel({
    statusLabel: document.getElementById("mappingIndexStatusLabel"),
    statusPill: document.getElementById("mappingIndexStatusPill"),
    statusText: document.getElementById("mappingIndexStatusText"),
    metaContainer: document.getElementById("mappingIndexMeta"),
    diagnoseOutput: document.getElementById("mappingDiagnoseOutput"),
    triggerButton: document.getElementById("mappingIndexTriggerButton"),
    rebuildButton: document.getElementById("mappingIndexRebuildButton"),
    cancelButton: document.getElementById("mappingIndexCancelButton"),
    diagnoseButton: document.getElementById("mappingDiagnoseButton"),
    api: bootstrap.api,
    fetchJson,
    notify,
    onRefresh: async () => {
        await Promise.all([loadDays(), loadStats()]);
        if (state.showingAllRoutes) {
            await loadAllRoutes();
            return;
        }
        if (state.currentDate) {
            await loadDay(state.currentDate, state.currentTripId || undefined);
        }
    },
});

function setEnabledTypes(events) {
    const available = new Set(events.map((event) => event.event_type || "unknown"));
    if (state.enabledTypes.size === 0) {
        available.forEach((eventType) => state.enabledTypes.add(eventType));
        return;
    }
    Array.from(state.enabledTypes).forEach((eventType) => {
        if (!available.has(eventType)) {
            state.enabledTypes.delete(eventType);
        }
    });
    available.forEach((eventType) => {
        if (!state.enabledTypes.has(eventType)) {
            state.enabledTypes.add(eventType);
        }
    });
}

function renderCurrentView() {
    eventsPanel.renderTrips(state.currentTrips, state.currentTripId);
    eventsPanel.renderFilters(filterSummaryRows(state.currentEvents));
    if (state.showingAllRoutes) {
        mapRenderer.renderAllRoutes({
            trips: state.currentTrips,
            events: state.currentEvents,
            enabledTypes: state.enabledTypes,
            label: "All routes",
        });
        return;
    }
    mapRenderer.renderDay({
        trips: state.currentTrips,
        events: state.currentEvents,
        enabledTypes: state.enabledTypes,
        label: formatDayLabel(state.currentDate),
    });
}

async function loadStats() {
    const [stats, drivingStats, charts] = await Promise.all([
        fetchJson(bootstrap.api.stats),
        fetchJson(bootstrap.api.driving_stats),
        fetchJson(bootstrap.api.event_charts),
    ]);
    state.stats = stats;
    state.drivingStats = drivingStats;
    state.charts = charts;
    eventsPanel.renderStats(stats, drivingStats, charts);
}

async function loadSentryEvents() {
    const payload = await fetchJson(bootstrap.api.sentry_events);
    sentryEvents.splice(0, sentryEvents.length, ...(payload.events || []));
    sentryInspector.renderSentryEvents(sentryEvents);
}

async function loadDays() {
    const payload = await fetchJson(`${bootstrap.api.days}?limit=365&min_distance=0`);
    state.days = payload.days || [];
    state.latestDate = state.days[0]?.date || "";
    eventsPanel.renderDays(state.days, state.currentDate, state.showingAllRoutes);
    return state.days;
}

async function loadDay(date, selectedTripId) {
    mapRenderer.setLoading(true);
    try {
        const [routes, events] = await Promise.all([
            fetchJson(replaceTemplate(bootstrap.api.day_routes_template, { __DATE__: date })),
            fetchJson(`${bootstrap.api.events}?date=${encodeURIComponent(date)}&limit=5000&overview=1`),
        ]);
        state.showingAllRoutes = false;
        state.currentDate = date;
        state.currentTrips = routes.trips || [];
        state.currentEvents = events.events || [];
        state.currentTripId = selectedTripId || state.currentTrips[0]?.trip_id || null;
        setEnabledTypes(state.currentEvents);
        renderCurrentView();
        eventsPanel.renderDays(state.days, state.currentDate, state.showingAllRoutes);
        if (state.currentTripId) {
            const trip = state.currentTrips.find((candidate) => (candidate.trip_id || candidate.id) === state.currentTripId);
            if (trip) {
                mapRenderer.focusTrip(trip);
            }
        }
    } finally {
        mapRenderer.setLoading(false);
    }
}

async function loadAllRoutes() {
    mapRenderer.setLoading(true);
    try {
        const [routes, events] = await Promise.all([
            fetchJson(`${bootstrap.api.all_routes}?max_points=400`),
            fetchJson(`${bootstrap.api.events}?overview=1&limit=5000`),
        ]);
        state.showingAllRoutes = true;
        state.currentTrips = routes.trips || [];
        state.currentEvents = events.events || [];
        state.currentTripId = null;
        setEnabledTypes(state.currentEvents);
        renderCurrentView();
        eventsPanel.renderDays(state.days, state.currentDate, state.showingAllRoutes);
    } finally {
        mapRenderer.setLoading(false);
    }
}

document.getElementById("mappingRefreshButton")?.addEventListener("click", async () => {
    try {
        await Promise.all([loadStats(), loadSentryEvents(), loadDays()]);
        if (state.showingAllRoutes) {
            await loadAllRoutes();
        } else if (state.currentDate) {
            await loadDay(state.currentDate, state.currentTripId || undefined);
        }
        notify("Mapping data refreshed.", "success");
    } catch (error) {
        notify(error.message, "warning");
    }
});

document.getElementById("mappingAllRoutesButton")?.addEventListener("click", () => {
    loadAllRoutes().catch((error) => notify(error.message, "warning"));
});

document.getElementById("mappingPrevDayButton")?.addEventListener("click", () => {
    const index = state.days.findIndex((day) => day.date === state.currentDate);
    const next = state.days[index + 1];
    if (next) {
        loadDay(next.date).catch((error) => notify(error.message, "warning"));
    }
});

document.getElementById("mappingNextDayButton")?.addEventListener("click", () => {
    const index = state.days.findIndex((day) => day.date === state.currentDate);
    const next = index > 0 ? state.days[index - 1] : null;
    if (next) {
        loadDay(next.date).catch((error) => notify(error.message, "warning"));
    }
});

document.getElementById("mappingToggleLegendButton")?.addEventListener("click", () => {
    const legend = document.getElementById("mappingSpeedLegend");
    legend.hidden = !legend.hidden;
});

document.getElementById("mappingFitAllButton")?.addEventListener("click", () => {
    mapRenderer.fitCurrent();
});

document.getElementById("mappingZoomSelectedButton")?.addEventListener("click", () => {
    mapRenderer.fitSelected();
});

window.addEventListener("resize", () => mapRenderer.invalidateSize());

Promise.all([loadStats(), loadSentryEvents(), loadDays()])
    .then(async () => {
        if (state.showingAllRoutes) {
            await loadAllRoutes();
            return;
        }
        const initialDate = bootstrap.view.date || state.latestDate;
        if (initialDate) {
            await loadDay(initialDate);
        } else {
            mapRenderer.clear();
        }
    })
    .catch((error) => {
        notify(error.message, "warning");
        mapRenderer.clear();
    });

indexerPanel.start();
