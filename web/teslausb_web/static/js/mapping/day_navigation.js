
function makeEventIcon(eventType) {
    const cfg = eventMarkerSvgs[eventType] || { color: '#6c757d', icon: '<circle cx="16" cy="13" r="5" fill="#fff"/>' };
    const id = 'ds_' + eventType;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 42" width="34" height="44">` +
        `<defs><filter id="${id}"><feDropShadow dx="0" dy="1.5" stdDeviation="1.5" flood-opacity=".3"/></filter></defs>` +
        `<g filter="url(#${id})">` +
        `<circle cx="16" cy="14.5" r="13.5" fill="${cfg.color}" stroke="#fff" stroke-width="2"/>` +
        `<polygon points="11,26 16,37 21,26" fill="${cfg.color}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>` +
        `</g>` +
        `${cfg.icon}` +
        `</svg>`;
    return L.divIcon({
        html: svg,
        className: 'event-svg-icon',
        iconSize: [34, 44],
        iconAnchor: [17, 43],
        popupAnchor: [0, -40],
    });
}

// --- Data Loading ---

async function loadStats() {
    try {
        const resp = await fetch(BOOTSTRAP.api.stats);
        const data = await resp.json();
        const indexedEl = document.getElementById('tripIndexedCount');
        if (indexedEl) indexedEl.textContent = (data.mapped_file_count || data.indexed_file_count || 0).toLocaleString();
    } catch(e) { console.error('Failed to load stats:', e); }
}

async function loadDays() {
    // Refresh the day navigator. Preserves the user's current
    // selection if it still exists; otherwise jumps to the most
    // recent day. The indexer-refresh hook calls this whenever
    // background indexing changes the row count, so we have to
    // be careful not to clobber an in-flight day load — that's
    // what the dayLoadSeq token in loadDay() is for.
    try {
        const resp = await fetch(withTz(BOOTSTRAP.api.days + '?limit=365'));
        const data = await resp.json();
        allDays = data.days || [];

        if (!allDays.length) {
            renderEmptyState();
            return;
        }

        // Restore current selection if it still exists; otherwise
        // jump to the most recent day.
        const stillExists = currentDate &&
            allDays.some(d => d.date === currentDate);
        if (!stillExists) {
            currentDate = allDays[0].date;
            await loadDay(currentDate);
        } else if (!currentDayData || !currentDayData.trips || !currentDayData.trips.length) {
            // initFromUrl() already kicked off loadDay() for the
            // bootstrapped date; only re-fetch if that hasn't landed
            // (or returned empty). Avoids a duplicate round-trip on
            // the common case where /api/days returns AFTER /api/day/
            // <date>/payload.
            await loadDay(currentDate);
        } else {
            // Day already loaded — but the initial renderDayCard()
            // ran before allDays was populated, so it painted "—"
            // and disabled both chevrons. Re-paint now that we know
            // the trip/event counts and edge-state.
            renderDayCard();
        }
    } catch (e) {
        console.error('Failed to load days:', e);
    }
}

async function loadDay(date, options) {
    // Render all trips and events for ``date`` in a single pass.
    // Increments dayLoadSeq so any in-flight earlier loadDay() call
    // discards its results when it returns — this prevents the
    // indexer-refresh poll from overwriting a day the user just
    // navigated to with stale data from the previous day.
    //
    // ``options.pushHistory`` (default false) — when true the URL
    // change goes onto the browser back stack; callers triggered by
    // rapid cycling pass nothing to keep history clean.
    // ``options.skipHistory`` (default false) — true only when the
    // popstate handler is restoring state from the URL.
    if (!date) return;
    const opts = options || {};
    closeDisambigPopup();
    // Drop the previous day's playability snapshot so a stale
    // entry can never filter out a real trip on the new day.
    // loadPlayableTripsForCurrentDay() repopulates this below.
    playableTripsForDay = null;
    currentDate = date;
    writeUrlState({ date: date }, {
        pushHistory: !!opts.pushHistory,
        skipHistory: !!opts.skipHistory,
    });
    const seq = ++dayLoadSeq;

    renderDayCard();  // immediate paint from cached metadata

    // Clear stale layers up-front so the user doesn't see the
    // previous day's routes/events while the new day loads (and so
    // a fetch failure doesn't leave inconsistent map state).
    tripLayer.clearLayers();
    fsdLayer.clearLayers();
    eventCluster.clearLayers();

    beginMapLoading();
    try {
        // Prefer the single-shot day payload (trips + events in one
        // round-trip with server-side RDP-simplified polylines and a
        // process-level cache). Fall back to the legacy two-fetch pair
        // when the bootstrap doesn't expose the payload template, e.g.
        // an older deployed backend.
        let routesData;
        let eventsData;
        if (BOOTSTRAP.api.day_payload_template) {
            const payloadResp = await fetch(
                withTz(BOOTSTRAP.api.day_payload_template.replace('__DATE__', encodeURIComponent(date))),
            );
            const payload = await payloadResp.json();
            routesData = { trips: payload.trips || [] };
            eventsData = { events: payload.events || [] };
        } else {
            // No date-scoped event cap: a busy sentry day can have
            // hundreds of events and silently truncating would hide
            // markers the day card promises. Server caps at 5000.
            const [routesResp, eventsResp] = await Promise.all([
                fetch(withTz(BOOTSTRAP.api.day_routes_template.replace('__DATE__', encodeURIComponent(date)))),
                fetch(withTz(`${BOOTSTRAP.api.events}?date=${encodeURIComponent(date)}&limit=5000`)),
            ]);
            routesData = await routesResp.json();
            eventsData = await eventsResp.json();
        }

        // Race guard: only apply if we're still the latest load AND
        // the user didn't navigate away while we were waiting.
        if (seq !== dayLoadSeq || date !== currentDate) return;

        currentDayData = routesData && routesData.trips ? routesData : { trips: [] };
        allEvents = eventsData.events || [];
        rebuildEventFilterPills();
        // Kick off the playability snapshot in parallel with
        // renderDay() — the disambiguation popup only needs it on
        // user click, and renderDay() doesn't depend on it. A late
        // arrival is fine; filterPlayableCandidates() fails-open
        // until the snapshot lands. Race-guarded by ``seq``.
        loadPlayableTripsForCurrentDay(date, seq);
        await renderDay();
    } catch (e) {
        console.error('Failed to load day routes/events:', e);
        if (seq !== dayLoadSeq || date !== currentDate) return;
        // Keep layers cleared (already done above) and reset cached
        // payloads so a subsequent successful load starts clean.
        currentDayData = { trips: [] };
        allEvents = [];
        rebuildEventFilterPills();
        if (typeof showToast === 'function') {
            showToast('Could not load this day. Try again.', 'warning');
        }
    } finally {
        endMapLoading();
        // Only re-render the day card if we're still the active load
        // — a stale finally must not re-enable nav buttons that a
        // newer in-flight loadDay() has intentionally disabled. The
        // active load will run its own renderDayCard() when it lands.
        if (seq === dayLoadSeq && date === currentDate) {
            renderDayCard();
        }
    }
}

function renderDayCard() {
    const day = allDays.find(d => d.date === currentDate);
    const dateEl = document.getElementById('dayCardDate');
    const statsEl = document.getElementById('dayCardStats');
    const prevBtn = document.getElementById('dayPrev');
    const nextBtn = document.getElementById('dayNext');

    if (!day) {
        dateEl.textContent = currentDate ? formatDayLabel(currentDate) : 'No data';
        statsEl.textContent = '\u2014';
        // currentDate isn't in allDays (e.g. user navigated to a day
        // with no indexed trips). Still allow nav by jumping to the
        // nearest available day in either direction. allDays is sorted
        // newest-first, so the FIRST entry strictly older than
        // currentDate is the prev target, and the LAST entry strictly
        // newer than currentDate is the next target.
        if (prevBtn) prevBtn.disabled = !(currentDate && allDays.some(d => d.date < currentDate));
        if (nextBtn) nextBtn.disabled = !(currentDate && allDays.some(d => d.date > currentDate));
        return;
    }

    dateEl.textContent = formatDayLabel(day.date);

    const parts = [];
    if (day.trip_count > 0) {
        const dist = (day.total_distance_km * 0.621371).toFixed(1);
        parts.push(`${day.trip_count} trip${day.trip_count === 1 ? '' : 's'}`);
        parts.push(`${dist} mi`);
    }
    if (day.video_count > 0) {
        parts.push(`${day.video_count} video${day.video_count === 1 ? '' : 's'}`);
    }
    if (day.event_count > 0) {
        parts.push(`${day.event_count} event${day.event_count === 1 ? '' : 's'}`);
    }
    statsEl.textContent = parts.length ? parts.join(' \u00B7 ') : '\u2014';

    // allDays is sorted newest-first: -1 = older (higher index),
    // +1 = newer (lower index). Disable buttons at the edges.
    const idx = allDays.findIndex(d => d.date === currentDate);
    if (prevBtn) prevBtn.disabled = (idx < 0 || idx >= allDays.length - 1);
    if (nextBtn) nextBtn.disabled = (idx <= 0);
}

function renderEmptyState() {
    const dateEl = document.getElementById('dayCardDate');
    const statsEl = document.getElementById('dayCardStats');
    const prevBtn = document.getElementById('dayPrev');
    const nextBtn = document.getElementById('dayNext');

    dateEl.textContent = 'No mapped drives yet';
    statsEl.textContent = 'Index dashcam clips to see trips and events';
    if (prevBtn) prevBtn.disabled = true;
    if (nextBtn) nextBtn.disabled = true;

    // Reset state so a stale load can't re-render against a now-empty
    // navigator and so filter pills don't show event types from a
    // previous populated state.
    currentDate = null;
    currentDayData = { trips: [] };
    allEvents = [];

    tripLayer.clearLayers();
    fsdLayer.clearLayers();
    eventCluster.clearLayers();
    rebuildEventFilterPills();
}

function formatDayLabel(date) {
    // ``date`` is a YYYY-MM-DD string from the server. Build a
    // local-tz Date object so the weekday is correct for the
    // viewer; constructing from "YYYY-MM-DDT00:00" avoids the
    // UTC parsing trap that bare ``new Date('2026-05-04')`` falls
    // into (which interprets as UTC and shifts the weekday in
    // negative-offset timezones).
    if (!date) return '';
    const parts = date.split('-');
    if (parts.length !== 3) return date;
    const d = new Date(
        parseInt(parts[0], 10),
        parseInt(parts[1], 10) - 1,
        parseInt(parts[2], 10),
    );
    if (isNaN(d.getTime())) return date;
    return d.toLocaleDateString(undefined, {
        weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
    });
}

function cycleDay(direction) {
    // direction: -1 = older day (cycle to next-higher index in
    // newest-first list); +1 = newer day.
    if (!allDays.length) return;

    const idx = allDays.findIndex(d => d.date === currentDate);
    if (idx < 0) {
        // currentDate isn't in allDays (date with no indexed trips).
        // allDays is sorted newest-first. Jump to the nearest available
        // day in the requested direction so prev/next chevrons behave
        // intuitively even when the user is parked on an empty day.
        if (direction < 0) {
            // older: first entry strictly older than currentDate.
            const target = allDays.find(d => d.date < currentDate);
            if (target) loadDay(target.date);
        } else {
            // newer: LAST entry strictly newer than currentDate.
            let target = null;
            for (const d of allDays) {
                if (d.date > currentDate) target = d; else break;
            }
            if (target) loadDay(target.date);
        }
        return;
    }
    const newIdx = idx - direction;  // -1 = older = +1 idx, etc.
    if (newIdx < 0 || newIdx >= allDays.length) return;
    loadDay(allDays[newIdx].date);
}

function selectDayForTrip(tripId) {
    // Used by the video panel "Trips" tab when the user taps a
    // trip row. We don't always know which day the trip belongs
    // to (videos panel may show trips outside the loaded day's
    // routes), so fetch the route, derive the date from the
    // first waypoint, and switch to that day. Fast taps on
    // multiple trips are protected by tripSelectSeq so a slower
    // earlier response can't overwrite a later one.
    const seq = ++tripSelectSeq;
    loadTripRoute(tripId).then(function (geojson) {
        if (seq !== tripSelectSeq) return;
        if (!geojson || !geojson.properties || !geojson.properties.waypoints) {
            return;
        }
        const wps = geojson.properties.waypoints;
        if (!wps.length || !wps[0].timestamp) return;
        const date = localDayOf(wps[0].timestamp);
        if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;

        // If the trip's day is older than the /api/days window
        // (e.g. user has 365+ days of data and clicked an old
        // trip in the videos panel), inject a synthetic stub so
        // cycleDay() and renderDayCard() don't disable the nav.
        // The stub is best-effort metadata; real stats land on
        // the next loadDays() refresh.
        if (!allDays.some(d => d.date === date)) {
            allDays.push({
                date: date,
                trip_count: 1,
                total_distance_km: geojson.properties.distance_km || 0,
                event_count: 0,
                sentry_count: 0,
            });
            allDays.sort((a, b) => b.date.localeCompare(a.date));
        }
        loadDay(date, { pushHistory: true });
    }).catch(function (e) {
        if (seq !== tripSelectSeq) return;
        console.error('Failed to resolve trip day:', e);
    });
    // Close the video panel so the map is visible.
    if (videoPanelOpen) toggleVideoPanel();
}

async function loadTripRoute(tripId) {
    try {
        const resp = await fetch(BOOTSTRAP.api.trip_route_template.replace('__TRIP_ID__', encodeURIComponent(String(tripId))));
        return await resp.json();
    } catch (e) {
        console.error('Failed to load route:', e);
        return null;
    }
}

// --- Event Filter Pills ---

function rebuildEventFilterPills() {
    // Pills are built per-day from the actually-present event
    // types, so users only see filters that are relevant. The
    // "All" pill at the front is a master toggle that turns
    // everything on or off.
    const container = document.getElementById('eventFilterPills');
    if (!container) return;
    container.replaceChildren();

    if (!allEvents.length) return;

    const counts = new Map();
    for (const ev of allEvents) {
        const t = ev.event_type || 'unknown';
        counts.set(t, (counts.get(t) || 0) + 1);
    }
    const types = Array.from(counts.keys()).sort();

    // "All" master pill
    const allOn = isAllEventsEnabled(types);
    container.appendChild(makePill({
        label: 'All', count: allEvents.length, type: null,
        active: allOn, color: 'var(--accent-primary, #3B82F6)',
        master: true,
    }));

    for (const t of types) {
        const cfg = eventMarkerSvgs[t] || { color: '#6c757d' };
        container.appendChild(makePill({
            label: t.replace(/_/g, ' '), count: counts.get(t), type: t,
            active: isTypeEnabled(t), color: cfg.color,
        }));
    }
}

function makePill({ label, count, type, active, color, master }) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'event-filter-pill' + (active ? ' active' : '') +
                    (master ? ' master' : '');
    if (active) btn.style.backgroundColor = color;
    const labelSpan = document.createElement('span');
    labelSpan.textContent = label;
    btn.appendChild(labelSpan);
    const countSpan = document.createElement('span');
    countSpan.className = 'pill-count';
    countSpan.textContent = String(count);
    btn.appendChild(countSpan);
    btn.addEventListener('click', () => toggleEventFilter(type));
    return btn;
}

function isTypeEnabled(t) {
    return enabledEventTypes === null || enabledEventTypes.has(t);
}

function isAllEventsEnabled(presentTypes) {
    if (enabledEventTypes === null) return true;
    return presentTypes.every(t => enabledEventTypes.has(t));
}

function toggleEventFilter(type) {
    const presentTypes = Array.from(new Set(
        allEvents.map(e => e.event_type || 'unknown')));

    if (type === null) {
        // Master toggle: if all on, turn all off; else turn all on.
        if (isAllEventsEnabled(presentTypes)) {
            enabledEventTypes = new Set();
        } else {
            enabledEventTypes = null;
        }
    } else {
        if (enabledEventTypes === null) {
            enabledEventTypes = new Set(presentTypes);
        }
        if (enabledEventTypes.has(type)) enabledEventTypes.delete(type);
        else enabledEventTypes.add(type);
    }
    persistEventTypeFilter();
    rebuildEventFilterPills();
    renderEvents();
}

function persistEventTypeFilter() {
    try {
        if (enabledEventTypes === null) {
            localStorage.removeItem(EVENT_TYPES_STORAGE_KEY);
        } else {
            localStorage.setItem(
                EVENT_TYPES_STORAGE_KEY,
                JSON.stringify(Array.from(enabledEventTypes)),
            );
        }
    } catch (e) { /* localStorage may be disabled; ignore */ }
}

function toggleSpeedLegend() {
    speedLegendVisible = !speedLegendVisible;
    const el = document.getElementById('speedLegend');
    const btn = document.getElementById('btnSpeedLegend');
    if (el) {
        el.classList.toggle('visible', speedLegendVisible);
        el.setAttribute('aria-hidden', speedLegendVisible ? 'false' : 'true');
    }
    if (btn) btn.classList.toggle('active', speedLegendVisible);
}
