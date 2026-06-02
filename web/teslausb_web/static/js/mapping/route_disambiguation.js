
function speedColor(mps) {
    const speed = displaySpeedValue(mps);
    const buckets = activeSpeedBuckets();
    for (const bucket of buckets) if (speed < bucket.max) return bucket.color;
    return buckets[buckets.length - 1].color;
}

// ===========================================================
// Disambiguation popup
// ===========================================================
//
// When the user taps a road segment that multiple trips have
// driven over, we want to show a list of candidate trips so they
// can pick the one whose video they actually want. The match is
// done in pixel space (not waypoint distance) so a click between
// sparse RDP-simplified waypoints still correctly identifies the
// trips passing through that point.
//
// Tapping a row opens the video overlay for that trip's nearest
// playable waypoint.

const DISAMBIG_PIXEL_RADIUS = 22;  // ~ matches the 14px clickTarget stroke + a small fudge for fat-finger

function pointSegDistPx(p, a, b) {
    // Distance from container point ``p`` to the line segment a→b
    // in container-pixel space. All three are L.Point. Using
    // pixels avoids the need to convert a search radius from
    // pixels to meters at every zoom level — and it's how a user
    // perceives distance on the screen anyway.
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const lenSq = dx * dx + dy * dy;
    if (lenSq === 0) {
        const px = p.x - a.x, py = p.y - a.y;
        return Math.sqrt(px * px + py * py);
    }
    let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / lenSq;
    if (t < 0) t = 0; else if (t > 1) t = 1;
    const projX = a.x + t * dx;
    const projY = a.y + t * dy;
    const px = p.x - projX, py = p.y - projY;
    return Math.sqrt(px * px + py * py);
}

function findCandidatesNearClick(clickLatLng, pixelRadius) {
    // Returns one candidate per visible trip whose closest segment
    // is within ``pixelRadius`` pixels of the click. Picks from the
    // currently-loaded day. Sort: newest first by start_time;
    // tie-break by distance.
    const radius = (pixelRadius == null) ? DISAMBIG_PIXEL_RADIUS : pixelRadius;
    const trips = (currentDayData && currentDayData.trips) || [];
    if (!trips.length) return [];

    const clickPt = map.latLngToContainerPoint(clickLatLng);
    const radius2 = radius * radius;

    const candidates = [];
    for (const trip of trips) {
        const wps = trip.waypoints || [];
        if (wps.length < 1) continue;

        // Project every waypoint to container space once. For a
        // trip with hundreds of waypoints this is O(n) per click,
        // which is fine on the user's phone (the Pi only serves
        // the JSON; this code runs in the browser).
        const projected = [];
        let minLeft = Infinity, minTop = Infinity;
        let maxLeft = -Infinity, maxTop = -Infinity;
        for (const wp of wps) {
            if (!Number.isFinite(wp.lat) || !Number.isFinite(wp.lon)) {
                projected.push(null);
                continue;
            }
            const pt = map.latLngToContainerPoint([wp.lat, wp.lon]);
            projected.push(pt);
            if (pt.x < minLeft) minLeft = pt.x;
            if (pt.x > maxLeft) maxLeft = pt.x;
            if (pt.y < minTop) minTop = pt.y;
            if (pt.y > maxTop) maxTop = pt.y;
        }
        // Cheap pixel-bounds reject: if the click + radius
        // doesn't overlap the trip's pixel bounding box, skip
        // the per-segment check entirely.
        if (clickPt.x < minLeft - radius || clickPt.x > maxLeft + radius
                || clickPt.y < minTop - radius || clickPt.y > maxTop + radius) {
            continue;
        }

        // Walk segments between consecutive valid waypoints.
        // Track the minimum segment distance and which waypoint
        // index was the segment endpoint nearest to the click —
        // that's what we'll use to seed the video overlay.
        let bestDist = Infinity;
        let bestWpIdx = -1;
        for (let i = 0; i < projected.length - 1; i++) {
            const a = projected[i];
            const b = projected[i + 1];
            if (!a || !b) continue;
            const d = pointSegDistPx(clickPt, a, b);
            if (d < bestDist) {
                bestDist = d;
                // Pick whichever segment endpoint is closer to
                // the click as the seed waypoint.
                const da2 = (clickPt.x - a.x) * (clickPt.x - a.x)
                          + (clickPt.y - a.y) * (clickPt.y - a.y);
                const db2 = (clickPt.x - b.x) * (clickPt.x - b.x)
                          + (clickPt.y - b.y) * (clickPt.y - b.y);
                bestWpIdx = (da2 <= db2) ? i : i + 1;
            }
        }
        // Single-waypoint trip (no segments) — fall back to the
        // single point's distance.
        if (bestWpIdx === -1 && projected[0]) {
            const a = projected[0];
            const d2 = (clickPt.x - a.x) * (clickPt.x - a.x)
                     + (clickPt.y - a.y) * (clickPt.y - a.y);
            if (d2 <= radius2) {
                bestDist = Math.sqrt(d2);
                bestWpIdx = 0;
            }
        }
        if (bestWpIdx >= 0 && bestDist <= radius) {
            candidates.push({
                trip: trip,
                nearestWpIdx: bestWpIdx,
                distance: bestDist,
            });
        }
    }

    candidates.sort((a, b) => {
        const ta = (a.trip && a.trip.start_time) || '';
        const tb = (b.trip && b.trip.start_time) || '';
        if (ta && tb && ta !== tb) return tb.localeCompare(ta);
        if (ta && !tb) return -1;
        if (!ta && tb) return 1;
        return a.distance - b.distance;
    });
    return candidates;
}

function pickPlayableWaypointForTrip(trip, clickLatLng) {
    // Find the nearest waypoint with a video_path so the video
    // overlay always opens to a real clip.
    const wps = (trip && trip.waypoints) || [];
    if (!wps.length) return null;
    const clickPt = map.latLngToContainerPoint(clickLatLng);
    let bestDist2 = Infinity, best = null;
    for (const wp of wps) {
        if (!wp || !wp.video_path) continue;
        if (!Number.isFinite(wp.lat) || !Number.isFinite(wp.lon)) continue;
        const pt = map.latLngToContainerPoint([wp.lat, wp.lon]);
        const dx = clickPt.x - pt.x, dy = clickPt.y - pt.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < bestDist2) { bestDist2 = d2; best = wp; }
    }
    return best;
}

function filterPlayableCandidates(candidates) {
    // Drop candidates whose trip is a "ghost" — its waypoints'
    // video_path values still point at clips Tesla has overwritten,
    // so the disambiguation popup would offer them and the user's
    // pick would yield a "No video available" toast. The server's
    // /api/trips/playable endpoint authoritatively answers this via
    // os.path.isfile() on the canonical paths (issue #77).
    //
    // Fail-open: if the playability snapshot hasn't arrived yet
    // (slow network, fetch errored), pass the candidate list through
    // unchanged so we never hide a real trip because of a metadata
    // fetch hiccup. Worst case: the user sees the same toast they
    // would have seen pre-fix.
    if (!candidates || !candidates.length) return candidates || [];
    if (!playableTripsForDay
        || playableTripsForDay.date !== currentDate
        || !playableTripsForDay.trips) {
        return candidates;
    }
    const known = playableTripsForDay.trips;
    const filtered = [];
    for (const c of candidates) {
        const tid = c && c.trip && c.trip.trip_id;
        if (tid == null) {
            // Trip without an id (defensive) — keep it; we have no
            // basis to filter.
            filtered.push(c);
            continue;
        }
        // The server JSON-stringifies trip ids (object keys must be
        // strings). Look up by string form; treat unknown trips as
        // playable to avoid hiding any trip the snapshot missed.
        const entry = known[String(tid)];
        if (entry === false) continue;
        filtered.push(c);
    }
    return filtered;
}

async function loadPlayableTripsForCurrentDay(date, seq) {
    // Fetch /api/trips/playable for ``date`` and stash the result in
    // playableTripsForDay so the disambiguation chooser can hide
    // ghost trips (issue #77). Race-guarded against rapid day
    // navigation: the caller passes the same dayLoadSeq token used
    // by loadDay(), and we only commit if it's still current.
    //
    // The endpoint is cached server-side for 60 s, so repeated
    // navigation back to the same day is essentially free.
    if (!date) return;
    let data;
    try {
        const resp = await fetch(
            withTz(`${BOOTSTRAP.api.playable_trips}?date=${encodeURIComponent(date)}`),
        );
        if (!resp.ok) {
            // Non-200 (e.g. 503 when image is missing or 400 on bad
            // date) — leave snapshot null so filtering fails-open.
            return;
        }
        data = await resp.json();
    } catch (e) {
        // Network error — same fail-open behavior.
        console.warn('Failed to load playable-trips snapshot:', e);
        return;
    }
    if (seq !== dayLoadSeq || date !== currentDate) return;
    if (data && data.date === date && data.trips
            && typeof data.trips === 'object') {
        playableTripsForDay = { date: date, trips: data.trips };
    }
}

function highlightCandidateTrip(trip) {
    // Draw a fresh thick polyline on a dedicated layer for the
    // candidate. Doesn't touch any existing trip layers, so we
    // never have to "restore" original styles — just clear the
    // highlight layer. Per the design system the accent color
    // works in both dark and light mode.
    disambigHighlightLayer.clearLayers();
    if (!trip) return;
    const wps = trip.waypoints || [];
    const latLngs = [];
    for (const wp of wps) {
        if (Number.isFinite(wp.lat) && Number.isFinite(wp.lon)) {
            latLngs.push([wp.lat, wp.lon]);
        }
    }
    if (latLngs.length < 2) return;
    L.polyline(latLngs, {
        renderer: sharedCanvasRenderer,
        color: '#3B82F6',
        weight: 6,
        opacity: 0.9,
        interactive: false,
    }).addTo(disambigHighlightLayer);
}

function clearDisambigHighlight() {
    disambigHighlightLayer.clearLayers();
}

function closeDisambigPopup() {
    // Idempotent cleanup. Called from navigation, layer clears,
    // popup close, and before opening the video overlay so a
    // stale popup never shadows the new context.
    clearDisambigHighlight();
    if (disambigPopupOpen) {
        disambigPopupOpen = false;
        map.closePopup();
    }
}

function buildDisambigRow(candidate, onPick) {
    // Build the popup row as DOM nodes (no innerHTML for any
    // user-derived text) so trip metadata can never sneak HTML
    // into the page.
    const trip = candidate.trip;
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'disambig-row';

    const main = document.createElement('div');
    main.className = 'disambig-row-main';
    const primary = document.createElement('div');
    primary.className = 'disambig-row-primary';
    primary.textContent = formatLocalTime(trip.start_time);
    main.appendChild(primary);

    const secondaryParts = [];
    const distMi = (typeof trip.distance_km === 'number')
        ? (trip.distance_km * 0.621371).toFixed(1) + ' mi'
        : null;
    const durMin = (typeof trip.duration_seconds === 'number')
        ? Math.round(trip.duration_seconds / 60) + ' min'
        : null;
    if (distMi) secondaryParts.push(distMi);
    if (durMin) secondaryParts.push(durMin);
    if (secondaryParts.length) {
        const secondary = document.createElement('div');
        secondary.className = 'disambig-row-secondary';
        secondary.textContent = secondaryParts.join(' \u00B7 ');
        main.appendChild(secondary);
    }
    row.appendChild(main);

    const chevron = document.createElement('span');
    chevron.className = 'disambig-row-chevron';
    chevron.setAttribute('aria-hidden', 'true');
    chevron.textContent = '\u203A';  // single right-pointing angle, not a Lucide icon
    row.appendChild(chevron);

    row.addEventListener('mouseenter', () => highlightCandidateTrip(trip));
    row.addEventListener('focus', () => highlightCandidateTrip(trip));
    row.addEventListener('mouseleave', clearDisambigHighlight);
    row.addEventListener('blur', clearDisambigHighlight);
    row.addEventListener('click', (ev) => {
        L.DomEvent.stopPropagation(ev);
        onPick(candidate);
    });
    return row;
}

function showDisambigPopup(latlng, candidates) {
    // Open the disambiguation popup at ``latlng``. Caller has
    // already verified candidates.length >= 2.
    closeDisambigPopup();

    const container = document.createElement('div');

    const header = document.createElement('div');
    header.className = 'disambig-header';
    header.textContent = candidates.length + ' clips through here';
    container.appendChild(header);

    const list = document.createElement('div');
    list.className = 'disambig-list';

    const onPick = (candidate) => {
        const trip = candidate.trip;
        // Open the video overlay for the trip's nearest playable
        // waypoint. Use the original click latlng so we pick the
        // same point the user actually tapped — not the candidate's
        // seed waypoint, which may be off-segment.
        const playable = pickPlayableWaypointForTrip(trip, latlng);
        const seed = playable
            || (trip.waypoints && trip.waypoints[candidate.nearestWpIdx])
            || null;
        if (!seed || !seed.video_path) {
            closeDisambigPopup();
            if (typeof showToast === 'function') {
                showToast('No video available for this trip near this point.', 'warning');
            }
            return;
        }
        const screenPt = map.latLngToContainerPoint(latlng);
        closeDisambigPopup();
        openVideoOverlay(seed, screenPt, trip.waypoints || [], trip.trip_id);
    };

    for (const c of candidates) {
        list.appendChild(buildDisambigRow(c, onPick));
    }
    container.appendChild(list);

    L.popup({
        className: 'disambig-popup',
        closeOnClick: true,
        autoClose: true,
        closeButton: true,
        keepInView: true,
        offset: L.point(0, -4),
    })
    .setLatLng(latlng)
    .setContent(container)
    .openOn(map);

    disambigPopupOpen = true;
    // Single one-shot listener guarantees the highlight gets
    // cleaned up regardless of how the popup closes (close
    // button, outside click, Escape, programmatic close).
    map.once('popupclose', () => {
        disambigPopupOpen = false;
        clearDisambigHighlight();
    });
}

const severityColors = {
    critical: '#dc3545',
    warning: '#ffc107',
    info: '#17a2b8',
};

// Balloon-pin marker SVGs: circular colored bubble with white skeuomorphic icon + pointer tail
// Icons centered at (16,13) inside 32x42 viewBox — designed to look like actual car controls
const eventMarkerSvgs = {
    // Brake pedal — rectangular pedal shape pressed down
    harsh_braking: { color: '#dc3545', icon:
        '<rect x="11" y="6" width="10" height="5" rx="1.5" fill="#fff"/>' +  // pedal pad
        '<rect x="14" y="11" width="4" height="7" rx="1" fill="#fff"/>' +    // pedal arm
        '<line x1="12" y1="20" x2="20" y2="20" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' // floor
    },
    // Brake pedal with !! — emergency
    emergency_braking: { color: '#b91c1c', icon:
        '<rect x="11" y="6" width="10" height="5" rx="1.5" fill="#fff"/>' +
        '<rect x="14" y="11" width="4" height="5" rx="1" fill="#fff"/>' +
        '<line x1="12" y1="18" x2="20" y2="18" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' +
        '<line x1="16" y1="6.5" x2="16" y2="9" stroke="#b91c1c" stroke-width="2" stroke-linecap="round"/>' + // !
        '<circle cx="16" cy="10" r="0.8" fill="#b91c1c"/>'
    },
    // Gas/accelerator pedal — side profile: thin pedal hinged at floor, tilted forward (pressed)
    hard_acceleration: { color: '#16a34a', icon:
        '<path d="M19,6 L15,6 L11,18 L14,18 L17,9 L19,9 Z" fill="#fff"/>' +   // pedal shape (angled thin)
        '<line x1="11" y1="20" x2="21" y2="20" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' +  // floor
        '<circle cx="12" cy="18.5" r="1.5" fill="none" stroke="#fff" stroke-width="1.5"/>'  // hinge pivot
    },
    // Steering wheel turned — wheel with 3 spokes, rotated
    sharp_turn: { color: '#f59e0b', icon:
        '<g transform="rotate(-30,16,13)">' +
        '<circle cx="16" cy="13" r="7.5" fill="none" stroke="#fff" stroke-width="2.5"/>' +
        '<circle cx="16" cy="13" r="2" fill="#fff"/>' +
        '<line x1="16" y1="5.5" x2="16" y2="11" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="9.5" y1="16.5" x2="14.2" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="22.5" y1="16.5" x2="17.8" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
        '</g>'
    },
    // Steering wheel with "A" badge — FSD/autopilot engaged
    autopilot_engaged: { color: '#3b82f6', icon:
        '<circle cx="16" cy="13" r="7.5" fill="none" stroke="#fff" stroke-width="2.5"/>' +
        '<circle cx="16" cy="13" r="2" fill="#fff"/>' +
        '<line x1="16" y1="5.5" x2="16" y2="11" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="9.5" y1="16.5" x2="14.2" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="22.5" y1="16.5" x2="17.8" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
        '<circle cx="22" cy="7" r="4" fill="#fff"/>' +  // badge bg
        '<text x="22" y="9.5" text-anchor="middle" font-size="7" font-weight="bold" fill="#3b82f6" font-family="sans-serif">A</text>'
    },
    // Hand on wheel — driver takeover
    autopilot_disengaged: { color: '#f97316', icon:
        '<circle cx="16" cy="14" r="7" fill="none" stroke="#fff" stroke-width="2.2"/>' +
        '<line x1="16" y1="7" x2="16" y2="11.5" stroke="#fff" stroke-width="2"/>' +
        '<line x1="10" y1="17.5" x2="14" y2="15.5" stroke="#fff" stroke-width="2"/>' +
        '<line x1="22" y1="17.5" x2="18" y2="15.5" stroke="#fff" stroke-width="2"/>' +
        // Hand gripping bottom of wheel
        '<path d="M11.5,18 Q11.5,21 16,21 Q20.5,21 20.5,18" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>'
    },
    // Speedometer — half circle gauge with needle in red zone
    speed_limit_exceeded: { color: '#ec4899', icon:
        '<path d="M7,18 A9,9 0 1,1 25,18" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>' +
        '<line x1="16" y1="16" x2="21" y2="9" stroke="#fff" stroke-width="3" stroke-linecap="round"/>' + // needle pointing right (fast)
        '<circle cx="16" cy="16" r="2" fill="#fff"/>' +
        // Speed marks
        '<line x1="9" y1="10" x2="10.5" y2="11.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>' +
        '<line x1="16" y1="6.5" x2="16" y2="8.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>' +
        '<line x1="23" y1="10" x2="21.5" y2="11.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>'
    },
    // Eye — sentry mode
    sentry: { color: '#8b5cf6', icon:
        '<path d="M7,13 Q16,4 25,13 Q16,22 7,13 Z" fill="none" stroke="#fff" stroke-width="2.2"/>' +
        '<circle cx="16" cy="13" r="3.5" fill="#fff"/>' +
        '<circle cx="16" cy="13" r="1.8" fill="#8b5cf6"/>'
    },
    // Bookmark/flag — saved clip
    saved: { color: '#007bff', icon:
        '<path d="M10,6 L22,6 L22,21 L16,17.5 L10,21 Z" fill="none" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/>'
    },
};
