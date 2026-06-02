// --- Day Rendering ---
//
// renderDay() draws all trips for ``currentDate`` plus their event
// markers in one pass. The bound trip data lives on
// ``currentDayData.trips`` (already includes inline waypoints, so
// we don't fan out per-trip route fetches like the old
// trip-by-trip view did). Speed-bucketed polylines render on a
// shared canvas — see activeSpeedBuckets() / sharedCanvasRenderer above.

async function renderDay() {
    tripLayer.clearLayers();
    fsdLayer.clearLayers();
    clearVpLocateMarker();
    const bounds = [];

    const trips = currentDayData && currentDayData.trips ? currentDayData.trips : [];
    for (const trip of trips) {
        renderTripOnDay(trip, bounds);
    }

    // Render events first so we can include their coords in bounds.
    // Otherwise event-only days (sentry weekend at home) would show
    // markers but leave the viewport pinned wherever the user was.
    const eventBounds = renderEvents();
    for (const c of eventBounds) bounds.push(c);

    if (bounds.length > 0) {
        map.fitBounds(bounds, { padding: [30, 30] });
    }
}

function renderTripOnDay(trip, bounds) {
    const waypoints = trip.waypoints || [];
    if (!waypoints.length) return;

    // Filter to renderable waypoints (skip null/non-finite coords).
    const valid = waypoints.filter(wp =>
        Number.isFinite(wp.lat) && Number.isFinite(wp.lon));
    if (!valid.length) return;

    if (Number.isFinite(trip.start_lat) && Number.isFinite(trip.start_lon)) {
        bounds.push([trip.start_lat, trip.start_lon]);
    }
    if (Number.isFinite(trip.end_lat) && Number.isFinite(trip.end_lon)) {
        bounds.push([trip.end_lat, trip.end_lon]);
    }

    // Speed-colored polyline: walk valid waypoints, group adjacent
    // segments by speed bucket, emit one polyline per run. This
    // gives us color information without exploding the polyline
    // count.
    //
    // Polylines are also broken at any waypoint flagged ``gap_after``
    // by the backend (large time/space jump to the next waypoint —
    // missing clip, parking break, or SEI clock skew). Without this,
    // a 6-minute parking gap would render as a multi-km diagonal
    // straight line cutting across roads.
    const segments = [];
    let currentColor = null;
    let currentRun = null;
    for (let i = 0; i < valid.length; i++) {
        const wp = valid[i];
        const color = speedColor(wp.speed_mps);
        const gapBefore = i > 0 && !!valid[i - 1].gap_after;
        if (gapBefore) {
            // Gap split: close current run, start new run with NO seed
            // so the new polyline does not visually connect to the
            // previous one across the gap.
            if (currentRun && currentRun.length >= 2) {
                segments.push({ color: currentColor, latlngs: currentRun });
            }
            currentRun = [[wp.lat, wp.lon]];
            currentColor = color;
        } else if (color !== currentColor) {
            if (currentRun && currentRun.length >= 2) {
                segments.push({ color: currentColor, latlngs: currentRun });
            }
            // Color-bucket transition: seed the new run with the
            // previous endpoint so adjacent buckets visually connect.
            const seed = currentRun && currentRun.length
                ? [currentRun[currentRun.length - 1]] : [];
            currentRun = seed.concat([[wp.lat, wp.lon]]);
            currentColor = color;
        } else {
            currentRun.push([wp.lat, wp.lon]);
        }
    }
    if (currentRun && currentRun.length >= 2) {
        segments.push({ color: currentColor, latlngs: currentRun });
    }

    const allLatLngs = valid.map(wp => [wp.lat, wp.lon]);

    // Build per-chunk click targets (one invisible wide polyline per
    // gap-bounded chunk of valid waypoints). A single trip-wide click
    // target would still register clicks in the empty space between
    // segments — chunking matches the visible polyline behavior.
    const clickChunks = [];
    let chunk = [];
    for (let i = 0; i < valid.length; i++) {
        chunk.push(valid[i]);
        if (valid[i].gap_after || i === valid.length - 1) {
            if (chunk.length >= 2) clickChunks.push(chunk);
            chunk = [];
        }
    }

    for (const chunkWps of clickChunks) {
        const chunkLatLngs = chunkWps.map(wp => [wp.lat, wp.lon]);
        const clickTarget = L.polyline(chunkLatLngs, {
            renderer: sharedCanvasRenderer,
            color: '#000', weight: 14, opacity: 0,
            interactive: true,
        }).addTo(tripLayer);
        clickTarget.on('click', function (e) {
            // Search across ALL trips currently rendered for this day,
            // not just this one — overlapping commute segments need
            // disambiguation, otherwise Leaflet's hit-test silently
            // picks whichever trip's clickTarget happens to be on top
            // and the user can't choose the drive they wanted.
            const candidates = findCandidatesNearClick(e.latlng);
            // Drop "ghost" trips whose video files no longer exist on
            // disk so the popup never offers them and the user never
            // hits a "No video available" toast (issue #77). Filter
            // fails-open while the playability snapshot is loading.
            const playableCandidates = filterPlayableCandidates(candidates);
            if (playableCandidates.length >= 2) {
                showDisambigPopup(e.latlng, playableCandidates);
                return;
            }
            if (playableCandidates.length === 1) {
                // Exactly one trip survived the filter — skip the
                // popup and play directly. Use the original click
                // latlng (not the candidate's seed) so we pick the
                // same point the user actually tapped.
                const trip = playableCandidates[0].trip;
                const playable = pickPlayableWaypointForTrip(trip, e.latlng);
                if (playable) {
                    openVideoOverlay(playable, e.containerPoint, trip.waypoints || valid, trip.trip_id);
                }
                return;
            }
            // Zero playable candidates. If the spatial search did
            // find something but every match was a ghost, tell the
            // user. Otherwise (no spatial match at all) fall through
            // to the legacy per-chunk fallback — try this trip's
            // nearest playable waypoint.
            if (candidates.length > 0) {
                if (typeof showToast === 'function') {
                    showToast('No clips available for this location.', 'warning');
                }
                return;
            }
            const fallbackTrip = { waypoints: valid };
            const fallbackPlayable = pickPlayableWaypointForTrip(fallbackTrip, e.latlng);
            if (fallbackPlayable) {
                openVideoOverlay(fallbackPlayable, e.containerPoint, valid);
            }
        });
    }

    for (const seg of segments) {
        L.polyline(seg.latlngs, {
            renderer: sharedCanvasRenderer,
            color: seg.color, weight: 4, opacity: 0.9,
            interactive: false,
        }).addTo(tripLayer);
    }

    // FSD overlay segments — break on autopilot_state change OR on a
    // gap_after boundary so a long drive with stable autopilot state
    // doesn't render a single overlay line that crosses a gap.
    let segStart = 0;
    for (let i = 1; i <= valid.length; i++) {
        const isEnd = i === valid.length;
        const prevAp = valid[i - 1].autopilot_state;
        const gapAtPrev = !!valid[i - 1].gap_after;
        const curAp = isEnd ? null : valid[i].autopilot_state;
        if (isEnd || gapAtPrev || prevAp !== curAp) {
            const segCoords = allLatLngs.slice(segStart, i);
            if (segCoords.length >= 2) {
                const engaged = ['SELF_DRIVING', 'AUTOSTEER'].includes(
                    valid[segStart].autopilot_state);
                L.polyline(segCoords, {
                    renderer: sharedCanvasRenderer,
                    color: engaged ? '#28a745' : '#6c757d',
                    weight: 6, opacity: 0.7, interactive: false,
                }).addTo(fsdLayer);
            }
            segStart = i;
        }
    }

    // Start/end markers. Use the trip metadata coords if present,
    // otherwise fall back to the first/last valid waypoint.
    const dist = ((trip.distance_km || 0) * 0.621371).toFixed(1);
    const dur = Math.round((trip.duration_seconds || 0) / 60);
    const startCoord = (Number.isFinite(trip.start_lat) && Number.isFinite(trip.start_lon))
        ? [trip.start_lat, trip.start_lon] : allLatLngs[0];
    const endCoord = (Number.isFinite(trip.end_lat) && Number.isFinite(trip.end_lon))
        ? [trip.end_lat, trip.end_lon] : allLatLngs[allLatLngs.length - 1];

    L.circleMarker(startCoord, {
        radius: 7, fillColor: '#28a745', color: '#fff', weight: 2, fillOpacity: 0.9,
    }).bindPopup(
        `<strong>Trip #${trip.trip_id}</strong><br>` +
        `${formatLocalTime(trip.start_time)}<br>` +
        `${dist} mi \u00B7 ${dur} min`
    ).addTo(tripLayer);

    L.circleMarker(endCoord, {
        radius: 7, fillColor: '#dc3545', color: '#fff', weight: 2, fillOpacity: 0.9,
    }).bindPopup('Trip End').addTo(tripLayer);
}

// --- Video Player Overlay ---

const CAMERA_ANGLES = ['front', 'back', 'left_repeater', 'right_repeater', 'left_pillar', 'right_pillar'];
const CAMERA_LABELS = {
    'front': 'Front', 'back': 'Back',
    'left_repeater': 'Left', 'right_repeater': 'Right',
    'left_pillar': 'L Pillar', 'right_pillar': 'R Pillar'
};
// Directional Lucide icons matching each camera's view direction (top-down
// car perspective). Front faces forward (up), pillar cameras face the rear
// quarter (diagonal arrows). Keeps the switcher visually scannable.
const CAMERA_ICONS = {
    'front': 'icon-chevron-up',
    'back': 'icon-chevron-down',
    'left_repeater': 'icon-chevron-left',
    'right_repeater': 'icon-chevron-right',
    'left_pillar': 'icon-arrow-down-left',
    'right_pillar': 'icon-arrow-down-right'
};

let overlayWaypoints = [];
let overlayClips = [];
let overlayClipIdx = 0;
let overlayCurrentAngle = 'front';
let overlayBasePath = '';
