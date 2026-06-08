/**
 * Speed-unit conversion + viridis speed buckets — a faithful TypeScript port of
 * the legacy `static/js/mapping/speed_units.js`. The legacy module read the unit
 * once from a server-rendered `BOOTSTRAP.view.speed_units` and exposed no UI
 * control. The SPA keeps the EXACT same conversion math, edges, colors and
 * labels, but parameterises the unit so the TripMap screen can offer the
 * mph/kmh toggle required by spa.md §3 (a small functional addition, flagged in
 * the lane report).
 *
 * Telemetry stays canonical in m/s end-to-end; the unit only selects display
 * conversion + which fixed bucket edge-set colours a polyline segment.
 */

export type SpeedUnit = "mph" | "kph";

export const MPS_PER_MPH = 0.44704;
export const KPH_PER_MPH = 1.609344;

const SPEED_BUCKET_EDGES_MPH = [15, 30, 45, 60, 75];
const SPEED_BUCKET_EDGES_KPH = [25, 50, 75, 100, 125];

/** Viridis-derived 6-stop ramp (matches map.css --mapping-speed-bucket-*). */
export const SPEED_BUCKET_COLORS = [
  "#440154",
  "#3b528b",
  "#21918c",
  "#5ec962",
  "#fde725",
  "#fffacd",
] as const;

export interface SpeedBucket {
  /** Upper bound of the bucket, in the *display* unit (Infinity for the top). */
  max: number;
  /** Legend label, e.g. "15–30" or "75+" (en-dash, matching legacy). */
  label: string;
  /** Polyline colour for segments whose display speed falls in this bucket. */
  color: string;
}

function buildSpeedBuckets(edges: number[]): SpeedBucket[] {
  const buckets: SpeedBucket[] = [];
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

const SPEED_BUCKETS_MPH = buildSpeedBuckets(SPEED_BUCKET_EDGES_MPH);
const SPEED_BUCKETS_KPH = buildSpeedBuckets(SPEED_BUCKET_EDGES_KPH);

/** The fixed bucket set for a unit (mph or kph). */
export function activeSpeedBuckets(unit: SpeedUnit): SpeedBucket[] {
  return unit === "kph" ? SPEED_BUCKETS_KPH : SPEED_BUCKETS_MPH;
}

/** Convert canonical m/s into the chosen display unit's numeric value. */
export function displaySpeedValue(mps: number, unit: SpeedUnit): number {
  const value = typeof mps === "number" && Number.isFinite(mps) ? Math.abs(mps) : 0;
  const mph = value / MPS_PER_MPH;
  return unit === "kph" ? mph * KPH_PER_MPH : mph;
}

/** Display speed rounded to a whole number (legacy `formatDisplaySpeed`). */
export function formatDisplaySpeed(mps: number, unit: SpeedUnit): string {
  return String(Math.round(displaySpeedValue(mps, unit)));
}

/** Pick the bucket colour for a canonical m/s speed under a display unit. */
export function speedColor(mps: number, unit: SpeedUnit): string {
  const v = displaySpeedValue(mps, unit);
  const buckets = activeSpeedBuckets(unit);
  // Half-open intervals `[prev, max)` — matches legacy `speed < bucket.max`.
  for (const b of buckets) {
    if (v < b.max) return b.color;
  }
  return buckets[buckets.length - 1].color;
}

/** The bucket index (0..5) for a canonical m/s speed under a display unit. */
export function speedBucketIndex(mps: number, unit: SpeedUnit): number {
  const v = displaySpeedValue(mps, unit);
  const buckets = activeSpeedBuckets(unit);
  for (let i = 0; i < buckets.length; i++) {
    if (v < buckets[i].max) return i;
  }
  return buckets.length - 1;
}
