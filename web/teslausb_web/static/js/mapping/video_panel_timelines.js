
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

        const safeType = (ev.event_type || '').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/_/g, ' ');
        const safeDesc = (ev.description || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');

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

function toggleVideoPanel() {
    videoPanelOpen = !videoPanelOpen;
    document.getElementById('videoPanel').classList.toggle('open', videoPanelOpen);
    document.getElementById('btnVideos').classList.toggle('active', videoPanelOpen);
    if (videoPanelOpen) {
        if (vpCurrentTab === 'clips') { vpPage = 1; loadVideoList(); }
        else if (vpCurrentTab === 'trips') { loadTripsTimeline(); }
        else { loadSentryTimeline(); }
    }
}

function vpShowClips() {
    vpCurrentTab = 'clips';
    document.getElementById('vpTabClips').classList.add('active');
    document.getElementById('vpTabSentry').classList.remove('active');
    document.getElementById('vpTabTrips').classList.remove('active');
    document.getElementById('vpFolderRow').style.display = '';
    vpPage = 1;
    loadVideoList();
}

function vpShowSentry() {
    vpCurrentTab = 'sentry';
    document.getElementById('vpTabSentry').classList.add('active');
    document.getElementById('vpTabClips').classList.remove('active');
    document.getElementById('vpTabTrips').classList.remove('active');
    document.getElementById('vpFolderRow').style.display = 'none';
    loadSentryTimeline();
}

function vpShowTrips() {
    vpCurrentTab = 'trips';
    document.getElementById('vpTabTrips').classList.add('active');
    document.getElementById('vpTabSentry').classList.remove('active');
    document.getElementById('vpTabClips').classList.remove('active');
    document.getElementById('vpFolderRow').style.display = 'none';
    loadTripsTimeline();
}

async function loadSentryTimeline() {
    const list = document.getElementById('vpList');
    list.innerHTML = '<div class="vp-loading">Loading\u2026</div>';

    try {
        const resp = await fetch(BOOTSTRAP.api.sentry_events);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();

        if (!data.events || data.events.length === 0) {
            list.innerHTML = '<div class="vp-empty">No events detected yet</div>';
            return;
        }

        const events = data.events;
        // Build date range for summary
        const first = events[events.length - 1];
        const last = events[0];
        const fmtShort = function(iso) {
            if (!iso) return '';
            try {
                var d = new Date(iso.replace(' ', 'T'));
                return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
            } catch(e) { return ''; }
        };
        const rangeStr = events.length > 1
            ? fmtShort(first.timestamp) + ' \u2013 ' + fmtShort(last.timestamp)
            : fmtShort(last.timestamp);

        let html = '<div class="sentry-timeline">';
        html += '<div class="st-summary"><strong>' + events.length + ' Event' + (events.length !== 1 ? 's' : '') + '</strong> \u00b7 <span>' + rangeStr + '</span></div>';

        events.forEach(function(ev) {
            const evType = ev.event_type || '';
            // Map event types to display labels and dot classes
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
            const dotClass = info.dot;
            const typeLabel = info.label;
            const dateStr = formatLocalTime(ev.timestamp);
            const folder = (ev.event_folder || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            const sourceFolder = (ev.source_folder || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

            // Driving events have description; sentry/saved have clip counts
            const isFolderEvent = (evType === 'sentry' || evType === 'saved');
            // Sentry/saved metadata is now lazy-loaded via /api/event-details
            // when the card scrolls into view; show a placeholder until it
            // arrives. This keeps the initial /api/sentry-events response
            // pure-DB and the panel responsive even with hundreds of events.
            let metaHtml = '';
            let metaIsLazy = false;
            if (isFolderEvent) {
                if (folder) {
                    metaHtml = '<span class="st-meta-loading">Loading details\u2026</span>';
                    metaIsLazy = true;
                } else {
                    // No event folder (rare — orphaned event row); nothing to fetch.
                    metaHtml = '';
                }
            } else {
                // Driving event — show description and severity
                const desc = ev.description || '';
                const sev = ev.severity || '';
                metaHtml = desc || sev;
            }

            const hasCoords = ev.lat != null && ev.lon != null && (ev.lat !== 0 || ev.lon !== 0);
            const videoPath = (ev.video_path || '').replace(/"/g, '&quot;');
            const frameOffset = ev.frame_offset || 0;

            html += '<div class="st-event" data-folder="' + sourceFolder + '" data-name="' + folder + '" data-video-path="' + videoPath + '" data-frame-offset="' + frameOffset + '" data-event-type="' + evType + '"';
            if (hasCoords) html += ' data-lat="' + ev.lat + '" data-lon="' + ev.lon + '"';
            html += '>';
            html += '<div class="st-dot ' + dotClass + '"></div>';
            html += '<div class="st-card">';
            html += '<div class="st-type">' + typeLabel + '</div>';
            html += '<div class="st-date">' + dateStr + '</div>';
            html += '<div class="st-meta"' + (metaIsLazy ? ' data-lazy-meta="1"' : '') + '>' + metaHtml + '</div>';
            html += '<div class="st-actions">';
            if (isFolderEvent) {
                // Sentry/saved events — always have a play button
                html += '<button class="vp-btn st-btn-play" type="button" title="Play" aria-label="Play event clip">' + ICON_PLAY + '</button>';
                html += '<button class="vp-btn st-btn-dl" type="button" title="Download ZIP" aria-label="Download event ZIP">' + ICON_DOWNLOAD + '</button>';
            } else if (hasCoords) {
                // Driving events with coordinates — show on map instead of play
                html += '<button class="vp-btn st-btn-map" type="button" title="Show on Map" aria-label="Show event on map" data-lat="' + ev.lat + '" data-lon="' + ev.lon + '">' + ICON_MAP_PIN + '</button>';
            } else if (videoPath) {
                // Driving events without coordinates — play button as fallback
                html += '<button class="vp-btn st-btn-play" type="button" title="Play" aria-label="Play event clip">' + ICON_PLAY + '</button>';
            }
            if (isFolderEvent) {
                html += '<button class="vp-btn vp-btn-danger st-btn-del" type="button" title="Delete" aria-label="Delete event">' + ICON_TRASH + '</button>';
            }
            html += '</div></div></div>';
        });

        html += '</div>';
        list.innerHTML = html;
        startLazyEventDetails(list);
    } catch(e) {
        console.error('Failed to load sentry timeline:', e);
        list.innerHTML = '<div class="vp-empty">Failed to load events</div>';
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

async function loadTripsTimeline() {
    const list = document.getElementById('vpList');
    list.innerHTML = '<div class="vp-loading">Loading\u2026</div>';

    try {
        const resp = await fetch(BOOTSTRAP.api.trips + '?limit=50');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();

        if (!data.trips || data.trips.length === 0) {
            list.innerHTML = '<div class="vp-empty">No trips indexed yet.<br><small>Trips are detected from dashcam GPS data when videos are indexed.</small></div>';
            return;
        }

        const trips = data.trips;
        let html = '<div class="sentry-timeline">';
        const totalVideos = trips.reduce(function(s, t) { return s + (t.video_count || 0); }, 0);
        html += '<div class="st-summary"><strong>' + trips.length + ' Trip' + (trips.length !== 1 ? 's' : '') + '</strong>' + (totalVideos > 0 ? ' \u00b7 <span>' + totalVideos + ' video' + (totalVideos !== 1 ? 's' : '') + '</span>' : '') + '</div>';

        trips.forEach(function(trip) {
            const dateStr = formatLocalTime(trip.start_time);
            const dist = trip.distance_km != null ? (trip.distance_km * 0.621371).toFixed(1) + ' mi' : '';
            const durMin = trip.duration_seconds != null ? Math.round(trip.duration_seconds / 60) : 0;
            const durStr = durMin > 0 ? durMin + ' min' : '';
            const evCount = trip.event_count || 0;
            const metaParts = [];
            if (dist) metaParts.push(dist);
            if (durStr) metaParts.push(durStr);
            if (evCount > 0) metaParts.push(evCount + ' event' + (evCount !== 1 ? 's' : ''));
            const metaHtml = metaParts.join(' \u00b7 ');

            const hasCoords = trip.start_lat != null && trip.start_lon != null;

            html += '<div class="st-event" data-trip-id="' + trip.id + '"';
            if (hasCoords) html += ' data-lat="' + trip.start_lat + '" data-lon="' + trip.start_lon + '"';
            html += '>';
            html += '<div class="st-dot trip"></div>';
            html += '<div class="st-card">';
            html += '<div class="st-type">' + (evCount > 0 ? '\u26A0 Trip with Events' : 'Trip') + '</div>';
            html += '<div class="st-date">' + dateStr + '</div>';
            html += '<div class="st-meta">' + metaHtml + '</div>';
            html += '<div class="st-actions">';
            if (hasCoords) {
                html += '<button class="vp-btn st-btn-map" type="button" title="Show on Map" aria-label="Show trip on map" data-lat="' + trip.start_lat + '" data-lon="' + trip.start_lon + '" data-trip-id="' + trip.id + '">' + ICON_MAP_PIN + '</button>';
            }
            html += '</div></div></div>';
        });

        html += '</div>';
        list.innerHTML = html;

        // Click handler: show trip route on map
        list.querySelectorAll('.st-event[data-trip-id]').forEach(function(el) {
            el.addEventListener('click', function(e) {
                if (e.target.closest('.vp-btn')) return;
                const tripId = el.dataset.tripId;
                if (tripId) selectDayForTrip(parseInt(tripId));
            });
        });
        list.querySelectorAll('.st-btn-map[data-trip-id]').forEach(function(btn) {
            btn.addEventListener('click', function() {
                const tripId = btn.dataset.tripId;
                if (tripId) selectDayForTrip(parseInt(tripId));
            });
        });
    } catch(e) {
        console.error('Failed to load trips:', e);
        list.innerHTML = '<div class="vp-empty">Failed to load trips</div>';
    }
}

