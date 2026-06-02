
function renderEvents() {
    // Returns an array of [lat, lon] coords for the rendered markers
    // so the caller can include them in fitBounds(). Event-only days
    // would otherwise leave the viewport unchanged.
    eventCluster.clearLayers();
    const bounds = [];

    allEvents.forEach(ev => {
        // Filter: skip events whose type is currently disabled,
        // and skip null/non-finite coords (would crash Leaflet).
        if (!isTypeEnabled(ev.event_type || 'unknown')) return;
        if (!Number.isFinite(ev.lat) || !Number.isFinite(ev.lon)) return;

        const marker = L.marker([ev.lat, ev.lon], {
            icon: makeEventIcon(ev.event_type),
        });

        const safeType = escapeHtml(ev.event_type || '').replace(/_/g, ' ');
        const safeDesc = escapeHtml(formatEventDescription(ev));

        marker.bindPopup(
            `<strong>${safeType}</strong><br>` +
            `${formatLocalTime(ev.timestamp)}<br>` +
            `${safeDesc}` +
            (ev.video_path ?
                `<br><a href="#" class="popup-video-link" data-trip-id="${ev.trip_id || 0}" data-video-path="${(ev.video_path || '').replace(/"/g, '&quot;')}" data-frame-offset="${ev.frame_offset || 0}" data-lat="${ev.lat}" data-lon="${ev.lon}">View Video</a>` : '')
        );

        eventCluster.addLayer(marker);
        bounds.push([ev.lat, ev.lon]);
    });

    return bounds;
}

// --- View Switching ---

function toggleFsdOverlay() {
    fsdVisible = !fsdVisible;
    document.getElementById('btnFsd').classList.toggle('active', fsdVisible);
    if (fsdVisible) {
        fsdLayer.addTo(map);
    } else {
        map.removeLayer(fsdLayer);
    }
}

// --- Indexer (legacy, removed in B-1) ---
// Indexing is performed by the Rust worker (teslausb-worker.service)
// against /var/lib/teslausb/index.sqlite3. The web UI is now a pure
// read-only consumer of that DB and no longer triggers or polls scans.

// --- Video Browser Panel ---
let videoPanelOpen = false;
let vpPage = 1;
let vpHasNext = false;
let vpFolderStructure = 'events';
let vpCurrentTab = 'sentry';
let vpLoading = false;
let vpScrollObserver = null;

const VP_TIMELINE_PAGE_LIMIT = 25;
let vpLoadSeq = 0;
let vpTimelineLoadedCount = 0;
let vpTimelineLoadedVideoCount = 0;
let vpTimelineFirstTimestamp = '';
let vpTimelineLastTimestamp = '';

function disconnectVpScrollSentinel() {
    if (vpScrollObserver) {
        vpScrollObserver.disconnect();
        vpScrollObserver = null;
    }
}

function resetVpPaging() {
    vpPage = 1;
    vpHasNext = false;
    vpLoading = false;
    vpLoadSeq += 1;
    disconnectVpScrollSentinel();
}

function attachVpScrollSentinel(sentinel, loadMore) {
    disconnectVpScrollSentinel();
    if (!('IntersectionObserver' in window)) {
        if (vpHasNext && !vpLoading) loadMore();
        return;
    }
    const root = document.getElementById('vpList');
    vpScrollObserver = new IntersectionObserver(function(entries) {
        for (var i = 0; i < entries.length; i++) {
            if (entries[i].isIntersecting && vpHasNext && !vpLoading) {
                disconnectVpScrollSentinel();
                loadMore();
                break;
            }
        }
    }, { root: root, rootMargin: '200px 0px', threshold: 0 });
    vpScrollObserver.observe(sentinel);
}

function appendVpScrollSentinel(list, loadMore) {
    var existingSentinel = list.querySelector('.vp-sentinel');
    if (existingSentinel) existingSentinel.remove();
    if (!vpHasNext) return;
    var sentinel = document.createElement('div');
    sentinel.className = 'vp-sentinel';
    sentinel.textContent = 'Loading more…';
    list.appendChild(sentinel);
    attachVpScrollSentinel(sentinel, loadMore);
}

function vpTimelineUrl(baseUrl) {
    return baseUrl + '?page=' + vpPage + '&limit=' + VP_TIMELINE_PAGE_LIMIT;
}

function toggleVideoPanel() {
    videoPanelOpen = !videoPanelOpen;
    document.getElementById('videoPanel').classList.toggle('open', videoPanelOpen);
    document.getElementById('btnVideos').classList.toggle('active', videoPanelOpen);
    if (videoPanelOpen) {
        if (vpCurrentTab === 'clips') { resetVpPaging(); loadVideoList(); }
        else if (vpCurrentTab === 'trips') { resetVpPaging(); loadTripsTimeline(); }
        else { resetVpPaging(); loadSentryTimeline(); }
    }
}

function vpShowClips() {
    vpCurrentTab = 'clips';
    document.getElementById('vpTabClips').classList.add('active');
    document.getElementById('vpTabSentry').classList.remove('active');
    document.getElementById('vpTabTrips').classList.remove('active');
    document.getElementById('vpFolderRow').style.display = '';
    resetVpPaging();
    loadVideoList();
}

function vpShowSentry() {
    vpCurrentTab = 'sentry';
    document.getElementById('vpTabSentry').classList.add('active');
    document.getElementById('vpTabClips').classList.remove('active');
    document.getElementById('vpTabTrips').classList.remove('active');
    document.getElementById('vpFolderRow').style.display = 'none';
    resetVpPaging();
    loadSentryTimeline();
}

function vpShowTrips() {
    vpCurrentTab = 'trips';
    document.getElementById('vpTabTrips').classList.add('active');
    document.getElementById('vpTabSentry').classList.remove('active');
    document.getElementById('vpTabClips').classList.remove('active');
    document.getElementById('vpFolderRow').style.display = 'none';
    resetVpPaging();
    loadTripsTimeline();
}

function fmtSentryShortDate(iso) {
    if (!iso) return '';
    try {
        var d = new Date(iso.replace(' ', 'T'));
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    } catch(e) { return ''; }
}

function updateSentrySummary(timeline) {
    const rangeStr = vpTimelineLoadedCount > 1
        ? fmtSentryShortDate(vpTimelineLastTimestamp) + ' – ' + fmtSentryShortDate(vpTimelineFirstTimestamp)
        : fmtSentryShortDate(vpTimelineFirstTimestamp);
    const suffix = vpHasNext ? '+' : '';
    const summary = timeline.querySelector('.st-summary');
    summary.innerHTML = '<strong>' + vpTimelineLoadedCount + suffix + ' Event' + (vpTimelineLoadedCount !== 1 ? 's' : '') + '</strong> · <span>' + rangeStr + '</span>';
}

function sentryEventHtml(ev) {
    const evType = ev.event_type || '';
    const eventLabels = {
        'sentry': { label: 'Sentry Mode', dot: 'sentry' },
        'saved': { label: 'Saved Clip', dot: 'saved' },
        'harsh_braking': { label: 'Hard Brake', dot: 'driving' },
        'emergency_braking': { label: 'Emergency Brake', dot: 'driving-critical' },
        'hard_acceleration': { label: 'Hard Acceleration', dot: 'driving' },
        'sharp_turn': { label: 'Sharp Turn', dot: 'driving' },
        'speed_limit_exceeded': { label: 'Speed Alert', dot: 'driving-critical' },
        'autopilot_disengaged': { label: 'Autopilot Disengaged', dot: 'fsd' },
        'autopilot_engaged': { label: 'Autopilot Engaged', dot: 'fsd' },
    };
    const info = eventLabels[evType] || { label: evType.replace(/_/g, ' '), dot: 'driving' };
    const folder = (ev.event_folder || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const sourceFolder = (ev.source_folder || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const isFolderEvent = (evType === 'sentry' || evType === 'saved');
    // A saved/sentry event is only *folder-backed* when it carries a real
    // event folder. Raw clip_events whose primary clip is missing (null FK)
    // surface as saved/sentry with an empty folder; rendering folder-style
    // Play/Download/Delete buttons for them produces no-op clicks and broken
    // ZIP/delete URLs. Such events still have a playable video_path, so they
    // fall through to the direct-video controls below.
    const folderBacked = isFolderEvent && !!folder;
    let metaHtml = '';
    let metaIsLazy = false;
    if (folderBacked) {
        metaHtml = '<span class="st-meta-loading">Loading details…</span>';
        metaIsLazy = true;
    } else {
        metaHtml = escapeHtml(formatEventDescription(ev) || ev.severity || '');
    }
    const hasCoords = ev.lat != null && ev.lon != null && (ev.lat !== 0 || ev.lon !== 0);
    const videoPath = (ev.video_path || '').replace(/"/g, '&quot;');
    const frameOffset = ev.frame_offset || 0;
    // Local-timezone day-bucket of this event, so "Show on Map" can load
    // the matching day (and its trip routes) before centering — the Events
    // tab is a global timeline, so an event may belong to a different day
    // than the one currently rendered. ev.timestamp is a UTC ISO string;
    // localDayOf converts it to the operator's local calendar day, which is
    // exactly the day key /api/day/* now buckets by.
    const evDate = localDayOf(ev.timestamp);
    let html = '<div class="st-event" data-folder="' + sourceFolder + '" data-name="' + folder + '" data-video-path="' + videoPath + '" data-frame-offset="' + frameOffset + '" data-event-type="' + evType + '" data-date="' + evDate + '"';
    if (hasCoords) html += ' data-lat="' + ev.lat + '" data-lon="' + ev.lon + '"';
    html += '><div class="st-dot ' + info.dot + '"></div><div class="st-card">';
    html += '<div class="st-type">' + info.label + '</div><div class="st-date">' + formatLocalTime(ev.timestamp) + '</div>';
    html += '<div class="st-meta"' + (metaIsLazy ? ' data-lazy-meta="1"' : '') + '>' + metaHtml + '</div><div class="st-actions">';
    // Show-on-Map locates the event and loads its day's trip routes (see the
    // st-btn-map handler). Offered whenever the event has finite coords, so
    // folder-backed saved/sentry events can jump to the route too — not only
    // the Play/Download/Delete folder controls.
    const mapBtnHtml = hasCoords
        ? '<button class="vp-btn st-btn-map" type="button" title="Show on Map" aria-label="Show event on map" data-lat="' + ev.lat + '" data-lon="' + ev.lon + '">' + ICON_MAP_PIN + '</button>'
        : '';
    if (folderBacked) {
        html += '<button class="vp-btn st-btn-play" type="button" title="Play" aria-label="Play event clip">' + ICON_PLAY + '</button>';
        html += mapBtnHtml;
        html += '<button class="vp-btn st-btn-dl" type="button" title="Download ZIP" aria-label="Download event ZIP">' + ICON_DOWNLOAD + '</button>';
    } else if (isFolderEvent) {
        if (videoPath) html += '<button class="vp-btn st-btn-play" type="button" title="Play" aria-label="Play event clip">' + ICON_PLAY + '</button>';
        html += mapBtnHtml;
    } else if (hasCoords) {
        html += mapBtnHtml;
    } else if (videoPath) {
        html += '<button class="vp-btn st-btn-play" type="button" title="Play" aria-label="Play event clip">' + ICON_PLAY + '</button>';
    }
    if (folderBacked) {
        html += '<button class="vp-btn vp-btn-danger st-btn-del" type="button" title="Delete" aria-label="Delete event">' + ICON_TRASH + '</button>';
    }
    return html + '</div></div></div>';
}

async function loadSentryTimeline(append) {
    if (vpLoading) return;
    const list = document.getElementById('vpList');
    const loadSeq = vpLoadSeq;
    if (!append) {
        list.innerHTML = '<div class="vp-loading">Loading…</div>';
        vpTimelineLoadedCount = 0;
        vpTimelineFirstTimestamp = '';
        vpTimelineLastTimestamp = '';
    }
    vpLoading = true;

    try {
        const resp = await fetch(vpTimelineUrl(BOOTSTRAP.api.sentry_events));
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (loadSeq !== vpLoadSeq || vpCurrentTab !== 'sentry') return;
        vpHasNext = !!data.has_next;

        if (!data.events || data.events.length === 0) {
            if (!append) list.innerHTML = '<div class="vp-empty">No events detected yet</div>';
            return;
        }

        let timeline = append ? list.querySelector('.sentry-timeline') : null;
        if (!timeline) {
            list.innerHTML = '<div class="sentry-timeline"><div class="st-summary"></div></div>';
            timeline = list.querySelector('.sentry-timeline');
        }
        data.events.forEach(function(ev) { timeline.insertAdjacentHTML('beforeend', sentryEventHtml(ev)); });
        vpTimelineLoadedCount += data.events.length;
        vpTimelineFirstTimestamp = vpTimelineFirstTimestamp || data.events[0].timestamp;
        vpTimelineLastTimestamp = data.events[data.events.length - 1].timestamp;
        updateSentrySummary(timeline);
        startLazyEventDetails(list);
        appendVpScrollSentinel(list, function() { vpPage++; loadSentryTimeline(true); });
    } catch(e) {
        console.error('Failed to load sentry timeline:', e);
        if (!append) list.innerHTML = '<div class="vp-empty">Failed to load events</div>';
    } finally {
        if (loadSeq === vpLoadSeq) vpLoading = false;
    }
}

// Lazy-fetch per-event details (clip count, camera count, size) only for
// cards that scroll into view. Earlier code did this enrichment server-side
// in a single /api/sentry-events call, which fired N filesystem ops up
// front and made the panel sluggish on a Pi Zero 2 W. The endpoint is
// now DB-only; this observer fills in the metadata on demand.
function startLazyEventDetails(container) {
    const cards = container.querySelectorAll('.st-event');
    if (!cards.length) return;

    const inflight = new Set();
    const fetchDetails = function(card) {
        const folder = card.getAttribute('data-folder');
        const name = card.getAttribute('data-name');
        if (!folder || !name) return;
        const key = folder + '/' + name;
        if (inflight.has(key)) return;
        inflight.add(key);

        const metaEl = card.querySelector('.st-meta[data-lazy-meta="1"]');
        if (!metaEl) return;

        fetch(BOOTSTRAP.api.event_details_template.replace('__FOLDER__', encodeURIComponent(folder)).replace('__EVENT__', encodeURIComponent(name)))
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(data) {
                if (!data || data.error) {
                    metaEl.textContent = '';
                    metaEl.removeAttribute('data-lazy-meta');
                    return;
                }
                const clips = data.clip_count != null ? data.clip_count : '?';
                const cams = data.camera_count != null ? data.camera_count : '?';
                const sizePart = data.size_mb ? Math.round(data.size_mb) + ' MB' : '';
                const parts = [clips + ' clips', cams + ' cameras'];
                if (sizePart) parts.push(sizePart);
                metaEl.textContent = parts.join(' \u00b7 ');
                metaEl.removeAttribute('data-lazy-meta');
            })
            .catch(function() {
                metaEl.textContent = '';
                metaEl.removeAttribute('data-lazy-meta');
            });
    };

    if (typeof IntersectionObserver === 'function') {
        const io = new IntersectionObserver(function(entries) {
            entries.forEach(function(e) {
                if (e.isIntersecting) {
                    fetchDetails(e.target);
                    io.unobserve(e.target);
                }
            });
        }, { root: container, rootMargin: '200px 0px' });
        cards.forEach(function(c) {
            if (c.querySelector('.st-meta[data-lazy-meta="1"]')) io.observe(c);
        });
    } else {
        // No IntersectionObserver — fall back to a simple "fetch the first
        // 20" pass. Better than blocking the server response on every event.
        Array.prototype.slice.call(cards, 0, 20).forEach(fetchDetails);
    }
}

function updateTripsSummary(timeline, totalVideos) {
    const suffix = vpHasNext ? '+' : '';
    const summary = timeline.querySelector('.st-summary');
    summary.innerHTML = '<strong>' + vpTimelineLoadedCount + suffix + ' Trip' + (vpTimelineLoadedCount !== 1 ? 's' : '') + '</strong>' + (totalVideos > 0 ? ' · <span>' + totalVideos + ' video' + (totalVideos !== 1 ? 's' : '') + '</span>' : '');
}

function tripEventHtml(trip) {
    const dist = trip.distance_km != null ? (trip.distance_km * 0.621371).toFixed(1) + ' mi' : '';
    const durMin = trip.duration_seconds != null ? Math.round(trip.duration_seconds / 60) : 0;
    const durStr = durMin > 0 ? durMin + ' min' : '';
    const evCount = trip.event_count || 0;
    const metaParts = [];
    if (dist) metaParts.push(dist);
    if (durStr) metaParts.push(durStr);
    if (evCount > 0) metaParts.push(evCount + ' event' + (evCount !== 1 ? 's' : ''));
    const hasCoords = trip.start_lat != null && trip.start_lon != null;
    let html = '<div class="st-event" data-trip-id="' + trip.id + '"';
    if (hasCoords) html += ' data-lat="' + trip.start_lat + '" data-lon="' + trip.start_lon + '"';
    html += '><div class="st-dot trip"></div><div class="st-card">';
    html += '<div class="st-type">' + (evCount > 0 ? 'Trip with Events' : 'Trip') + '</div>';
    html += '<div class="st-date">' + formatLocalTime(trip.start_time) + '</div>';
    html += '<div class="st-meta">' + metaParts.join(' · ') + '</div><div class="st-actions">';
    if (hasCoords) {
        html += '<button class="vp-btn st-btn-map" type="button" title="Show on Map" aria-label="Show trip on map" data-lat="' + trip.start_lat + '" data-lon="' + trip.start_lon + '" data-trip-id="' + trip.id + '">' + ICON_MAP_PIN + '</button>';
    }
    return html + '</div></div></div>';
}

function bindTripCardHandlers(root) {
    root.querySelectorAll('.st-event[data-trip-id]:not([data-trip-bound="1"])').forEach(function(el) {
        el.dataset.tripBound = '1';
        el.addEventListener('click', function(e) {
            if (e.target.closest('.vp-btn')) return;
            const tripId = el.dataset.tripId;
            if (tripId) selectDayForTrip(parseInt(tripId));
        });
    });
    root.querySelectorAll('.st-btn-map[data-trip-id]:not([data-trip-bound="1"])').forEach(function(btn) {
        btn.dataset.tripBound = '1';
        btn.addEventListener('click', function() {
            const tripId = btn.dataset.tripId;
            if (tripId) selectDayForTrip(parseInt(tripId));
        });
    });
}

async function loadTripsTimeline(append) {
    if (vpLoading) return;
    const list = document.getElementById('vpList');
    const loadSeq = vpLoadSeq;
    if (!append) {
        list.innerHTML = '<div class="vp-loading">Loading…</div>';
        vpTimelineLoadedCount = 0;
        vpTimelineLoadedVideoCount = 0;
    }
    vpLoading = true;

    try {
        const resp = await fetch(vpTimelineUrl(BOOTSTRAP.api.trips));
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (loadSeq !== vpLoadSeq || vpCurrentTab !== 'trips') return;
        vpHasNext = !!data.has_next;

        if (!data.trips || data.trips.length === 0) {
            if (!append) list.innerHTML = '<div class="vp-empty">No trips indexed yet.<br><small>Trips are detected from dashcam GPS data when videos are indexed.</small></div>';
            return;
        }

        let timeline = append ? list.querySelector('.sentry-timeline') : null;
        if (!timeline) {
            list.innerHTML = '<div class="sentry-timeline"><div class="st-summary"></div></div>';
            timeline = list.querySelector('.sentry-timeline');
        }
        data.trips.forEach(function(trip) { timeline.insertAdjacentHTML('beforeend', tripEventHtml(trip)); });
        vpTimelineLoadedCount += data.trips.length;
        vpTimelineLoadedVideoCount += data.total_video_count || 0;
        updateTripsSummary(timeline, vpTimelineLoadedVideoCount);
        bindTripCardHandlers(list);
        appendVpScrollSentinel(list, function() { vpPage++; loadTripsTimeline(true); });
    } catch(e) {
        console.error('Failed to load trips:', e);
        if (!append) list.innerHTML = '<div class="vp-empty">Failed to load trips</div>';
    } finally {
        if (loadSeq === vpLoadSeq) vpLoading = false;
    }
}

