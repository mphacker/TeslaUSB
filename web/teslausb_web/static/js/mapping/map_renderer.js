const EVENT_STYLES = {
    harsh_brake: { label: "Harsh brake", icon: "octagon-alert", token: "--msg-error-text", color: "#dc3545" },
    emergency_brake: { label: "Emergency brake", icon: "octagon-alert", token: "--msg-error-text", color: "#b91c1c" },
    hard_acceleration: { label: "Hard acceleration", icon: "zap", token: "--msg-success-text", color: "#16a34a" },
    sharp_turn: { label: "Sharp turn", icon: "rotate-cw", token: "--msg-warning-text", color: "#f59e0b" },
    fsd_engage: { label: "FSD engaged", icon: "bot", token: "--btn-primary-bg", color: "#3b82f6" },
    fsd_disengage: { label: "FSD disengaged", icon: "bot-off", token: "--msg-warning-text", color: "#f97316" },
    speeding: { label: "Speeding", icon: "gauge", token: "--btn-danger-bg", color: "#ec4899" },
    sentry: { label: "Sentry", icon: "shield-alert", token: "--msg-error-text", color: "#8b5cf6" },
    saved: { label: "Saved", icon: "bookmark", token: "--btn-info-bg", color: "#007bff" },
    autopilot: { label: "Autopilot", icon: "bot", token: "--msg-success-text", color: "#3b82f6" },
    unknown: { label: "Other", icon: "triangle-alert", token: "--text-secondary", color: "#6c757d" },
};

// V1-parity balloon-pin SVG bodies (centred inside 32x42 viewBox at 16,13).
// Each entry mirrors v1's eventMarkerSvgs so map markers look identical.
const EVENT_MARKER_SVGS = {
    harsh_brake:
        '<rect x="11" y="6" width="10" height="5" rx="1.5" fill="#fff"/>' +
        '<rect x="14" y="11" width="4" height="7" rx="1" fill="#fff"/>' +
        '<line x1="12" y1="20" x2="20" y2="20" stroke="#fff" stroke-width="2" stroke-linecap="round"/>',
    emergency_brake:
        '<rect x="11" y="6" width="10" height="5" rx="1.5" fill="#fff"/>' +
        '<rect x="14" y="11" width="4" height="5" rx="1" fill="#fff"/>' +
        '<line x1="12" y1="18" x2="20" y2="18" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' +
        '<line x1="16" y1="6.5" x2="16" y2="9" stroke="#b91c1c" stroke-width="2" stroke-linecap="round"/>' +
        '<circle cx="16" cy="10" r="0.8" fill="#b91c1c"/>',
    hard_acceleration:
        '<path d="M19,6 L15,6 L11,18 L14,18 L17,9 L19,9 Z" fill="#fff"/>' +
        '<line x1="11" y1="20" x2="21" y2="20" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' +
        '<circle cx="12" cy="18.5" r="1.5" fill="none" stroke="#fff" stroke-width="1.5"/>',
    sharp_turn:
        '<g transform="rotate(-30,16,13)">' +
        '<circle cx="16" cy="13" r="7.5" fill="none" stroke="#fff" stroke-width="2.5"/>' +
        '<circle cx="16" cy="13" r="2" fill="#fff"/>' +
        '<line x1="16" y1="5.5" x2="16" y2="11" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="9.5" y1="16.5" x2="14.2" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="22.5" y1="16.5" x2="17.8" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
        '</g>',
    fsd_engage:
        '<circle cx="16" cy="13" r="7.5" fill="none" stroke="#fff" stroke-width="2.5"/>' +
        '<circle cx="16" cy="13" r="2" fill="#fff"/>' +
        '<line x1="16" y1="5.5" x2="16" y2="11" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="9.5" y1="16.5" x2="14.2" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="22.5" y1="16.5" x2="17.8" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
        '<circle cx="22" cy="7" r="4" fill="#fff"/>' +
        '<text x="22" y="9.5" text-anchor="middle" font-size="7" font-weight="bold" fill="#3b82f6" font-family="sans-serif">A</text>',
    fsd_disengage:
        '<circle cx="16" cy="14" r="7" fill="none" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="16" y1="7" x2="16" y2="11.5" stroke="#fff" stroke-width="2"/>' +
        '<line x1="10" y1="17.5" x2="14" y2="15.5" stroke="#fff" stroke-width="2"/>' +
        '<line x1="22" y1="17.5" x2="18" y2="15.5" stroke="#fff" stroke-width="2"/>' +
        '<path d="M11.5,18 Q11.5,21 16,21 Q20.5,21 20.5,18" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>',
    speeding:
        '<path d="M7,18 A9,9 0 1,1 25,18" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>' +
        '<line x1="16" y1="16" x2="21" y2="9" stroke="#fff" stroke-width="3" stroke-linecap="round"/>' +
        '<circle cx="16" cy="16" r="2" fill="#fff"/>' +
        '<line x1="9" y1="10" x2="10.5" y2="11.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>' +
        '<line x1="16" y1="6.5" x2="16" y2="8.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>' +
        '<line x1="23" y1="10" x2="21.5" y2="11.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>',
    sentry:
        '<path d="M7,13 Q16,4 25,13 Q16,22 7,13 Z" fill="none" stroke="#fff" stroke-width="2.2"/>' +
        '<circle cx="16" cy="13" r="3.5" fill="#fff"/>' +
        '<circle cx="16" cy="13" r="1.8" fill="#8b5cf6"/>',
    saved:
        '<path d="M10,6 L22,6 L22,21 L16,17.5 L10,21 Z" fill="none" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/>',
};

function eventMarkerColor(eventType) {
    return (EVENT_STYLES[eventType] || EVENT_STYLES.unknown).color;
}

function makeEventBalloonIcon(eventType) {
    const color = eventMarkerColor(eventType);
    const iconBody = EVENT_MARKER_SVGS[eventType]
        || '<circle cx="16" cy="13" r="5" fill="#fff"/>';
    const id = `ds_${eventType || "unknown"}`;
    const svg =
        `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 42" width="34" height="44">` +
        `<defs><filter id="${id}"><feDropShadow dx="0" dy="1.5" stdDeviation="1.5" flood-opacity=".3"/></filter></defs>` +
        `<g filter="url(#${id})">` +
        `<circle cx="16" cy="14.5" r="13.5" fill="${color}" stroke="#fff" stroke-width="2"/>` +
        `<polygon points="11,26 16,37 21,26" fill="${color}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>` +
        `</g>${iconBody}</svg>`;
    return L.divIcon({
        html: svg,
        className: "event-balloon-icon",
        iconSize: [34, 44],
        iconAnchor: [17, 43],
        popupAnchor: [0, -40],
    });
}

function escapeHtml(value) {
    return String(value == null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function formatPopupTime(timestamp) {
    if (!timestamp) {
        return "";
    }
    try {
        return new Date(timestamp).toLocaleString();
    } catch (_err) {
        return String(timestamp);
    }
}

function popupHtmlForEvent(event) {
    const style = EVENT_STYLES[event.event_type] || EVENT_STYLES.unknown;
    const label = style.label;
    const desc = escapeHtml(event.description || "");
    const time = escapeHtml(formatPopupTime(event.timestamp));
    let html = `<strong>${escapeHtml(label)}</strong>`;
    if (time) {
        html += `<br>${time}`;
    }
    if (desc) {
        html += `<br>${desc}`;
    }
    if (event.video_path) {
        html +=
            `<br><a href="#" class="popup-video-link"` +
            ` data-trip-id="${escapeHtml(event.trip_id || 0)}"` +
            ` data-video-path="${escapeHtml(event.video_path)}"` +
            ` data-frame-offset="${escapeHtml(event.frame_offset || 0)}"` +
            ` data-lat="${escapeHtml(event.lat)}"` +
            ` data-lon="${escapeHtml(event.lon)}">View Video</a>`;
    }
    return html;
}

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
                const type = event.event_type || "unknown";
                const marker = L.marker([event.lat, event.lon], {
                    icon: makeEventBalloonIcon(type),
                    title: (EVENT_STYLES[type] || EVENT_STYLES.unknown).label,
                });
                marker.on("click", () => options.onSelectEvent(event));
                marker.bindPopup(popupHtmlForEvent(event));
                marker.on("popupopen", (popupEvent) => {
                    const root = popupEvent.popup.getElement();
                    if (!root) {
                        return;
                    }
                    const link = root.querySelector(".popup-video-link");
                    if (!link) {
                        return;
                    }
                    link.addEventListener("click", (clickEvent) => {
                        clickEvent.preventDefault();
                        options.onSelectEvent(event);
                    }, { once: true });
                });
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
