const EVENT_STYLES = {
    sentry: { label: "Sentry", icon: "shield-alert", token: "--msg-error-text" },
    saved: { label: "Saved", icon: "bookmark", token: "--btn-info-bg" },
    harsh_brake: { label: "Harsh brake", icon: "octagon-alert", token: "--msg-warning-text" },
    autopilot: { label: "Autopilot", icon: "bot", token: "--msg-success-text" },
    speeding: { label: "Speeding", icon: "gauge", token: "--btn-danger-bg" },
    unknown: { label: "Other", icon: "triangle-alert", token: "--text-secondary" },
};

const SPEED_BUCKETS = [
    { maxMph: 25, token: "--btn-info-bg" },
    { maxMph: 45, token: "--btn-primary-bg" },
    { maxMph: 65, token: "--msg-success-text" },
    { maxMph: Number.POSITIVE_INFINITY, token: "--msg-warning-text" },
];

function colorToken(name) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || "currentColor";
}

function styleForEvent(eventType) {
    return EVENT_STYLES[eventType] || EVENT_STYLES.unknown;
}

function markerHtml(eventType, spriteUrl) {
    const style = styleForEvent(eventType);
    const color = colorToken(style.token);
    return `
        <span style="display:grid;place-items:center;width:2rem;height:2rem;border-radius:999px;background:${color};color:var(--bg-secondary);border:1px solid var(--bg-secondary)">
            <svg aria-hidden="true" style="width:1rem;height:1rem"><use href="${spriteUrl}#icon-${style.icon}"></use></svg>
        </span>
    `;
}

function routeColor(speedMps) {
    const mph = Math.abs(Number(speedMps || 0) * 2.23694);
    const bucket = SPEED_BUCKETS.find((item) => mph <= item.maxMph) || SPEED_BUCKETS.at(-1);
    return colorToken(bucket.token);
}

function routeBoundsFromWaypoints(waypoints) {
    return waypoints
        .filter((waypoint) => Number.isFinite(waypoint.lat) && Number.isFinite(waypoint.lon))
        .map((waypoint) => [waypoint.lat, waypoint.lon]);
}

function splitWaypoints(waypoints) {
    const segments = [];
    let current = [];
    waypoints.forEach((waypoint, index) => {
        current.push(waypoint);
        if (waypoint.gap_after && index < waypoints.length - 1) {
            if (current.length > 1) {
                segments.push(current);
            }
            current = [waypoints[index + 1]];
        }
    });
    if (current.length > 1) {
        segments.push(current);
    }
    return segments;
}

function nearestPlayableWaypoint(trip, latlng) {
    const waypoints = Array.isArray(trip.waypoints) ? trip.waypoints : [];
    let nearest = null;
    let minDistance = Number.POSITIVE_INFINITY;
    waypoints.forEach((waypoint) => {
        if (!waypoint.video_path || !Number.isFinite(waypoint.lat) || !Number.isFinite(waypoint.lon)) {
            return;
        }
        const point = L.latLng(waypoint.lat, waypoint.lon);
        const distance = point.distanceTo(latlng);
        if (distance < minDistance) {
            minDistance = distance;
            nearest = waypoint;
        }
    });
    return nearest;
}

export function createMapRenderer(options) {
    const frame = options.frame;
    const mapElement = options.mapElement;
    const filterPills = options.filterPills;
    const modePill = options.modePill;
    const summaryText = options.summaryText;
    const routeChip = options.routeChip;
    const eventChip = options.eventChip;
    const spriteUrl = options.spriteUrl;

    if (window.L?.Icon?.Default) {
        window.L.Icon.Default.imagePath = options.leafletIconPath;
    }

    if ("serviceWorker" in navigator && options.tileCacheUrl) {
        navigator.serviceWorker.register(options.tileCacheUrl).catch(() => {});
    }

    const map = L.map(mapElement).setView([37.7749, -122.4194], 9);
    const gridLayer = L.gridLayer({ attribution: "Local route grid", maxZoom: 19 });
    gridLayer.createTile = () => {
        const tile = document.createElement("div");
        tile.style.background = "var(--bg-primary)";
        tile.style.border = "1px solid var(--border-color)";
        return tile;
    };
    gridLayer.addTo(map);

    const routeLayer = L.layerGroup().addTo(map);
    const eventCluster = L.markerClusterGroup({ maxClusterRadius: 42, spiderfyOnMaxZoom: true }).addTo(map);
    const focusLayer = L.layerGroup().addTo(map);
    let currentBounds = null;
    let selectedBounds = null;
    let currentTrips = [];
    let currentEvents = [];

    function setLoading(isLoading) {
        frame.classList.toggle("is-loading", isLoading);
        modePill.textContent = isLoading ? "Loading" : modePill.textContent;
    }

    function renderFilterPills(events, enabledTypes) {
        filterPills.replaceChildren();
        const counts = new Map();
        events.forEach((event) => {
            const eventType = event.event_type || "unknown";
            counts.set(eventType, (counts.get(eventType) || 0) + 1);
        });
        Array.from(counts.entries())
            .sort((left, right) => left[0].localeCompare(right[0]))
            .forEach(([eventType, count]) => {
                const style = styleForEvent(eventType);
                const button = document.createElement("button");
                button.type = "button";
                button.className = "mapping-filter-chip";
                button.dataset.eventType = eventType;
                button.style.setProperty("--pill-accent", colorToken(style.token));
                if (enabledTypes.has(eventType)) {
                    button.classList.add("is-active");
                }
                button.innerHTML = `<svg aria-hidden="true"><use href="${spriteUrl}#icon-${style.icon}"></use></svg><span>${style.label}</span><span>${count}</span>`;
                button.addEventListener("click", () => options.onToggleEventType(eventType));
                filterPills.append(button);
            });
        filterPills.hidden = counts.size === 0;
    }

    function renderEvents(events, enabledTypes) {
        eventCluster.clearLayers();
        const bounds = [];
        events
            .filter((event) => enabledTypes.has(event.event_type || "unknown"))
            .forEach((event) => {
                if (!Number.isFinite(event.lat) || !Number.isFinite(event.lon)) {
                    return;
                }
                const marker = L.marker([event.lat, event.lon], {
                    icon: L.divIcon({
                        className: "mapping-event-icon",
                        html: markerHtml(event.event_type || "unknown", spriteUrl),
                        iconSize: [32, 32],
                        iconAnchor: [16, 16],
                    }),
                    title: styleForEvent(event.event_type || "unknown").label,
                });
                marker.on("click", () => options.onSelectEvent(event));
                marker.bindPopup(`<strong>${styleForEvent(event.event_type || "unknown").label}</strong><br>${event.description || "No description"}`);
                eventCluster.addLayer(marker);
                bounds.push([event.lat, event.lon]);
            });
        return bounds;
    }

    function renderTrip(trip, isAllRoutes) {
        const waypoints = Array.isArray(trip.waypoints) ? trip.waypoints : [];
        const segments = splitWaypoints(waypoints.filter((waypoint) => Number.isFinite(waypoint.lat) && Number.isFinite(waypoint.lon)));
        const tripBounds = routeBoundsFromWaypoints(waypoints);
        segments.forEach((segment) => {
            const latlngs = segment.map((waypoint) => [waypoint.lat, waypoint.lon]);
            const polyline = L.polyline(latlngs, {
                color: routeColor(segment[Math.floor(segment.length / 2)]?.speed_mps),
                weight: isAllRoutes ? 4 : 6,
                opacity: isAllRoutes ? 0.62 : 0.82,
            });
            polyline.on("click", (mapEvent) => {
                options.onSelectTrip(trip, { fromAllRoutes: isAllRoutes });
                const waypoint = nearestPlayableWaypoint(trip, mapEvent.latlng);
                if (waypoint) {
                    options.onOpenClip({ trip, waypoint, source: "map" });
                }
            });
            routeLayer.addLayer(polyline);
        });
        return tripBounds;
    }

    function renderSummary(modeLabel, trips, events) {
        modePill.textContent = modeLabel;
        modePill.dataset.state = "idle";
        summaryText.textContent = `${trips.length} route(s) and ${events.length} event(s) are visible.`;
        routeChip.textContent = `${trips.length} routes`;
        eventChip.textContent = `${events.length} events`;
    }

    function updateBounds(bounds) {
        currentBounds = bounds.length > 0 ? L.latLngBounds(bounds) : null;
        if (currentBounds) {
            map.fitBounds(currentBounds.pad(0.15));
        }
    }

    function clearFocus() {
        focusLayer.clearLayers();
        selectedBounds = null;
    }

    function focusTrip(trip) {
        clearFocus();
        const bounds = routeBoundsFromWaypoints(Array.isArray(trip?.waypoints) ? trip.waypoints : []);
        if (bounds.length === 0) {
            return;
        }
        const highlight = L.polyline(bounds, {
            color: colorToken("--text-link"),
            weight: 8,
            opacity: 0.95,
        });
        focusLayer.addLayer(highlight);
        selectedBounds = L.latLngBounds(bounds);
        map.fitBounds(selectedBounds.pad(0.2));
    }

    function focusEvent(event) {
        if (!Number.isFinite(event?.lat) || !Number.isFinite(event?.lon)) {
            return;
        }
        clearFocus();
        const marker = L.circleMarker([event.lat, event.lon], {
            radius: 12,
            color: colorToken("--text-link"),
            weight: 3,
            fillOpacity: 0,
        });
        focusLayer.addLayer(marker);
        map.panTo([event.lat, event.lon]);
    }

    function fitCurrent() {
        if (currentBounds) {
            map.fitBounds(currentBounds.pad(0.15));
        }
    }

    function fitSelected() {
        if (selectedBounds) {
            map.fitBounds(selectedBounds.pad(0.2));
        }
    }

    function renderDay(payload) {
        routeLayer.clearLayers();
        clearFocus();
        currentTrips = Array.isArray(payload.trips) ? payload.trips : [];
        currentEvents = Array.isArray(payload.events) ? payload.events : [];
        const bounds = [];
        currentTrips.forEach((trip) => {
            bounds.push(...renderTrip(trip, false));
        });
        bounds.push(...renderEvents(currentEvents, payload.enabledTypes));
        renderFilterPills(currentEvents, payload.enabledTypes);
        renderSummary(payload.label, currentTrips, currentEvents.filter((event) => payload.enabledTypes.has(event.event_type || "unknown")));
        updateBounds(bounds);
    }

    function renderAllRoutes(payload) {
        routeLayer.clearLayers();
        clearFocus();
        currentTrips = Array.isArray(payload.trips) ? payload.trips : [];
        currentEvents = Array.isArray(payload.events) ? payload.events : [];
        const bounds = [];
        currentTrips.forEach((trip) => {
            bounds.push(...renderTrip(trip, true));
        });
        bounds.push(...renderEvents(currentEvents, payload.enabledTypes));
        renderFilterPills(currentEvents, payload.enabledTypes);
        renderSummary(payload.label, currentTrips, currentEvents.filter((event) => payload.enabledTypes.has(event.event_type || "unknown")));
        updateBounds(bounds);
    }

    return {
        map,
        setLoading,
        renderDay,
        renderAllRoutes,
        fitCurrent,
        fitSelected,
        focusTrip,
        focusEvent,
        invalidateSize() {
            map.invalidateSize();
        },
        clear() {
            routeLayer.clearLayers();
            eventCluster.clearLayers();
            clearFocus();
            currentTrips = [];
            currentEvents = [];
            filterPills.replaceChildren();
            renderSummary("Idle", [], []);
        },
    };
}
