// Stored telemetry stays canonical (`speed_mps`); BOOTSTRAP.view.speed_units
// only chooses display conversion and the fixed display-unit bucket set.
const SPEED_UNITS_MPH = 'mph';
const SPEED_UNITS_KPH = 'kph';
const MPS_PER_MPH = 0.44704;
const KPH_PER_MPH = 1.609344;
const SPEED_EVENT_TYPE = 'speed_limit_exceeded';
const SPEED_EVENT_DESCRIPTION_PATTERN =
    /^Speed (\d+(?:\.\d+)?) m\/s exceeded limit (\d+(?:\.\d+)?) m\/s$/;
const SPEED_BUCKET_EDGES_MPH = [15, 30, 45, 60, 75];
const SPEED_BUCKET_EDGES_KPH = [25, 50, 75, 100, 125];
const SPEED_BUCKET_COLORS = [
    '#440154',
    '#3b528b',
    '#21918c',
    '#5ec962',
    '#fde725',
    '#fffacd',
];
const SPEED_BUCKETS_MPH = buildSpeedBuckets(SPEED_BUCKET_EDGES_MPH);
const SPEED_BUCKETS_KPH = buildSpeedBuckets(SPEED_BUCKET_EDGES_KPH);
const MAP_SPEED_UNITS = readMapSpeedUnits();

function readMapSpeedUnits() {
    const configured = BOOTSTRAP.view && BOOTSTRAP.view.speed_units;
    return configured === SPEED_UNITS_KPH ? SPEED_UNITS_KPH : SPEED_UNITS_MPH;
}

function buildSpeedBuckets(edges) {
    const buckets = [];
    let previous = 0;
    edges.forEach((edge, index) => {
        buckets.push({
            max: edge,
            label: `${previous}\u2013${edge}`,
            color: SPEED_BUCKET_COLORS[index],
        });
        previous = edge;
    });
    buckets.push({
        max: Infinity,
        label: `${previous}+`,
        color: SPEED_BUCKET_COLORS[SPEED_BUCKET_COLORS.length - 1],
    });
    return buckets;
}

function activeSpeedBuckets() {
    return MAP_SPEED_UNITS === SPEED_UNITS_KPH ? SPEED_BUCKETS_KPH : SPEED_BUCKETS_MPH;
}

function speedUnitLabel() {
    return MAP_SPEED_UNITS;
}

function displaySpeedValue(mps) {
    const value = (typeof mps === 'number' && Number.isFinite(mps)) ? Math.abs(mps) : 0;
    const mph = value / MPS_PER_MPH;
    return MAP_SPEED_UNITS === SPEED_UNITS_KPH ? mph * KPH_PER_MPH : mph;
}

function formatDisplaySpeed(mps) {
    return String(Math.round(displaySpeedValue(mps)));
}

function formatEventDescription(ev) {
    const description = ev.description || '';
    if (ev.event_type !== SPEED_EVENT_TYPE) return description;
    const match = SPEED_EVENT_DESCRIPTION_PATTERN.exec(description);
    if (!match) return description;
    const speed = formatDisplaySpeed(Number.parseFloat(match[1]));
    const limit = formatDisplaySpeed(Number.parseFloat(match[2]));
    return `Speed ${speed} ${speedUnitLabel()} exceeded limit ${limit} ${speedUnitLabel()}`;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function renderSpeedLegend() {
    const legend = document.getElementById('speedLegend');
    if (!legend) return;
    const title = legend.querySelector('.speed-legend-title');
    if (title) title.textContent = `Speed (${speedUnitLabel()})`;
    const labels = legend.querySelectorAll('.speed-legend-row span:last-child');
    activeSpeedBuckets().forEach((bucket, index) => {
        const label = labels[index];
        if (label) label.textContent = bucket.label;
    });
}

renderSpeedLegend();
