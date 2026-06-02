const LUCIDE_SPRITE = BOOTSTRAP.assets.sprite;
const CLOUD_ARCHIVE_ENABLED = !!(BOOTSTRAP.features && BOOTSTRAP.features.cloud_archive_enabled);
const iconMarkup = (iconId, className = 'nav-icon') => `<svg class="${className}" aria-hidden="true"><use href="${LUCIDE_SPRITE}#${iconId}"></use></svg>`;
const ICON_CLOSE = iconMarkup('icon-x'), ICON_TRASH = iconMarkup('icon-trash-2'), ICON_DOWNLOAD = iconMarkup('icon-download'), ICON_CLOUD = iconMarkup('icon-cloud'), ICON_PLAY = iconMarkup('icon-play');
const ICON_MAP_PIN = iconMarkup('icon-map-pin'), ICON_CHEVRON_LEFT = iconMarkup('icon-chevron-left'), ICON_CHEVRON_RIGHT = iconMarkup('icon-chevron-right'), ICON_SPINNING = iconMarkup('icon-refresh-cw', 'nav-icon spinning-icon'), ICON_CHECK = iconMarkup('icon-check-circle');

// Register Service Worker for offline tile caching (gap 1)
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register(BOOTSTRAP.assets.tile_cache_sw, { scope: '/' })
        .then(reg => console.log('Tile cache SW registered'))
        .catch(err => console.warn('Tile cache SW failed:', err));
}

// Format ISO timestamp to local time
function formatLocalTime(isoStr) {
    if (!isoStr) return 'Unknown';
    try {
        const d = new Date(isoStr.replace(' ', 'T'));
        return d.toLocaleString(undefined, {
            month: 'short', day: 'numeric', year: 'numeric',
            hour: 'numeric', minute: '2-digit', hour12: true
        });
    } catch(e) { return 'Invalid Date'; }
}

// --- URL state (issue #57) ---
//
// Persist the selected day / view-mode in the query string so refresh,
// share, bookmark, and browser back/forward all work. State on the page
// (``currentDate``) is the source of truth for
// rendering; the URL is a serialized projection of that state.
//
// pushState vs replaceState policy:
//   - Rapid cycling (chevrons + keyboard arrows) uses ``replaceState``
//     so back/forward history doesn't flood with one entry per arrow.
//   - Deliberate cross-day jumps (trip-panel taps, disambig pick,
//     event-marker "Go to this day") use ``pushState`` so the user
//     can back out of an intentional navigation.
//   - The popstate handler restores state from the URL with
//     ``skipHistory: true`` so the restoration does not itself modify
//     the history stack.
function readUrlState() {
    try {
        const params = new URLSearchParams(window.location.search);
        const date = params.get('date');
        if (date && /^\d{4}-\d{2}-\d{2}$/.test(date)) {
            // Reject syntactically-valid but impossible dates (e.g.
            // 2026-13-99). Round-tripping through Date catches both
            // out-of-range months/days and Feb-29 in non-leap years.
            const parts = date.split('-');
            const y = parseInt(parts[0], 10);
            const m = parseInt(parts[1], 10);
            const d = parseInt(parts[2], 10);
            const dt = new Date(y, m - 1, d);
            if (!isNaN(dt.getTime()) &&
                dt.getFullYear() === y &&
                dt.getMonth() === m - 1 &&
                dt.getDate() === d) {
                return { date };
            }
        }
    } catch (e) { /* malformed URL — fall through to default */ }
    return null;
}

function writeUrlState(state, options) {
    const opts = options || {};
    if (opts.skipHistory) return;
    try {
        const params = new URLSearchParams();
        if (state && state.date && /^\d{4}-\d{2}-\d{2}$/.test(state.date)) {
            params.set('date', state.date);
        }
        const qs = params.toString();
        const url = qs ? (window.location.pathname + '?' + qs)
                       : window.location.pathname;
        const stateObj = {
            view: 'day',
            date: (state && state.date) || null,
        };
        if (opts.pushHistory) {
            window.history.pushState(stateObj, '', url);
        } else {
            window.history.replaceState(stateObj, '', url);
        }
    } catch (e) { /* history API unavailable (e.g. sandboxed iframe) */ }
}

// Fix Leaflet marker icon path for vendored setup
L.Icon.Default.imagePath = BOOTSTRAP.assets.leaflet_icon_path;

// --- Map Initialization ---
const map = L.map('map').setView([37.7749, -122.4194], 10);

// OpenStreetMap tiles (loaded from OSM servers; cached by browser for offline)
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
}).addTo(map);

// Layer groups
// Map renderer: canvas is dramatically faster than SVG for the
// dozens-of-polylines-per-trip workload introduced by speed-bucket
// rendering. We share a single canvas instance across all polylines.
const sharedCanvasRenderer = L.canvas({ padding: 0.2 });
const tripLayer = L.layerGroup().addTo(map);
const eventCluster = L.markerClusterGroup({ maxClusterRadius: 40, spiderfyOnMaxZoom: true }).addTo(map);
const fsdLayer = L.layerGroup();

let fsdVisible = false;
let speedLegendVisible = false;

// Day-based state (replaces the trip-by-trip model). ``allDays`` is
// the navigator source of truth, ordered newest-first; ``currentDate``
// is the YYYY-MM-DD currently rendered on the map; ``currentDayData``
// is the cached payload from /api/day/<date>/routes; ``allEvents`` is
// the matching /api/events?date= payload (kept separate so the filter
// pills can re-render without refetching). ``dayLoadSeq`` is a token
// used to discard out-of-order responses when the user cycles fast or
// the indexer-refresh hook fires mid-load.
// ``tripSelectSeq`` guards selectDayForTrip's pre-loadDay window — a
// fast tap on Trip A then Trip B in the video panel shouldn't let
// the slower A response win the race.
let allDays = [];
let currentDate = null;
let currentDayData = { trips: [] };
let allEvents = [];
let dayLoadSeq = 0;
let tripSelectSeq = 0;

// ── Day-load progress indicator ──
// Cheap nestable counter: callers wrap each in-flight load in a
// beginMapLoading() / endMapLoading() pair. The map-container's
// .is-loading class drops only when the counter reaches zero, so
// overlapping fetches keep the bar visible without flicker. A 150 ms
// reveal delay hides the bar entirely for instant loads (cached,
// sub-second), so the user only ever sees it when there's actually
// something to wait for.
let mapLoadingDepth = 0;
let mapLoadingRevealTimer = null;

function beginMapLoading() {
    mapLoadingDepth += 1;
    if (mapLoadingDepth !== 1) return;
    if (mapLoadingRevealTimer) {
        clearTimeout(mapLoadingRevealTimer);
    }
    mapLoadingRevealTimer = setTimeout(function () {
        mapLoadingRevealTimer = null;
        if (mapLoadingDepth <= 0) return;
        const container = document.querySelector('.map-container');
        const bar = document.getElementById('mapLoadingBar');
        if (container) container.classList.add('is-loading');
        if (bar) {
            bar.setAttribute('aria-busy', 'true');
            bar.setAttribute('aria-hidden', 'false');
        }
    }, 150);
    disableDayNav();
}

function endMapLoading() {
    mapLoadingDepth = Math.max(0, mapLoadingDepth - 1);
    if (mapLoadingDepth !== 0) return;
    if (mapLoadingRevealTimer) {
        clearTimeout(mapLoadingRevealTimer);
        mapLoadingRevealTimer = null;
    }
    const container = document.querySelector('.map-container');
    const bar = document.getElementById('mapLoadingBar');
    if (container) container.classList.remove('is-loading');
    if (bar) {
        bar.setAttribute('aria-busy', 'false');
        bar.setAttribute('aria-hidden', 'true');
    }
    // Re-enable of the nav buttons is the responsibility of renderDayCard()
    // — that caller knows the new view's edge-state (e.g. next stays
    // disabled on the most-recent day). Calling it is the only correct
    // way to restore the buttons.
}

function disableDayNav() {
    // Disable the prev/next buttons during in-flight loads so
    // the user can't queue a chain of clicks that race the seq guard.
    // The corresponding re-enable happens in renderDayCard() once the
    // load completes — that function sets the correct enabled state
    // for the new view.
    const prevBtn = document.getElementById('dayPrev');
    const nextBtn = document.getElementById('dayNext');
    if (prevBtn) prevBtn.disabled = true;
    if (nextBtn) nextBtn.disabled = true;
}

// Per-day playability snapshot for the disambiguation chooser.
// Shape: { date: 'YYYY-MM-DD', trips: { <trip_id>: <bool> } }.
// Populated by loadPlayableTripsForCurrentDay() after each loadDay()
// fetch lands. Read by filterPlayableCandidates() to drop ghost trips
// (waypoints reference video files Tesla has overwritten) before the
// disambiguation popup is built (issue #77). Null until the first
// load completes — filtering fails-open in that window so a slow
// network never hides real trips. Cleared whenever we leave the
// per-day view so stale state can't bleed into a subsequent day.
let playableTripsForDay = null;

// Highlight overlay used by the disambiguation popup to flash a
// candidate trip's polyline on row hover. Drawn into a dedicated
// layer group (not tripLayer) so we never have to "restore" the
// styles of existing polylines — clearing this layer is enough.
const disambigHighlightLayer = L.layerGroup().addTo(map);

// Tracks whether a disambiguation popup is open so we can
// cleanup highlight layers on navigation/render without depending
// on Leaflet's popupclose event firing in every code path.
let disambigPopupOpen = false;

// User-controllable per-event-type visibility. ``null`` means
// "show all"; otherwise it's a Set of enabled event types.
// Persisted to localStorage so the user's preferred filter state
// survives page reloads.
const EVENT_TYPES_STORAGE_KEY = 'mapping.enabledEventTypes';

let enabledEventTypes = (function () {
    try {
        const raw = localStorage.getItem(EVENT_TYPES_STORAGE_KEY);
        if (raw === null) return null;  // never set → show all
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? new Set(parsed) : null;
    } catch (e) { return null; }
})();

// Speed buckets in m/s mapped to viridis colors. Polyline segments
// are bucketed and each bucket emits run-length-encoded multi-line
// segments, NOT one polyline per waypoint pair (which would create
// thousands of Leaflet objects on a multi-trip day and crush the
// Pi Zero 2 W).
// MPH-native speed buckets. Stored telemetry is `speed_mps` but
// the UI is mph-only (US car), so we define the cut-points in
// mph and convert once at compare time. Granularity is chosen
// for typical US driving (parking → residential → city → 4-lane
// → highway → fast highway) so colour actually varies across
// the speeds people drive at, instead of collapsing 25-75 mph
// into a single bucket.
const MPS_PER_MPH = 0.44704;
const SPEED_BUCKETS_MPH = [
    { maxMph: 15, color: '#440154' },   //   0-15 mph   parking / walking pace
    { maxMph: 30, color: '#3b528b' },   //  15-30 mph   residential
    { maxMph: 45, color: '#21918c' },   //  30-45 mph   city / suburban
    { maxMph: 60, color: '#5ec962' },   //  45-60 mph   highway (lower)
    { maxMph: 75, color: '#fde725' },   //  60-75 mph   highway
    { maxMph: Infinity, color: '#fffacd' }, // 75+ mph
];
