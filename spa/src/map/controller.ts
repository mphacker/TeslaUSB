/**
 * TripMapController — the imperative Leaflet integration for the trip-map
 * screen. Leaflet is a non-Preact library, so it is driven through this plain
 * controller class created/destroyed from a Preact ref/effect (see
 * `screens/TripMap.tsx`). This establishes the clean "imperative lib behind a
 * ref" pattern future screens (Chart.js analytics, the dashcam-MP4 HUD) mirror.
 *
 * It is a faithful port of the legacy `static/js/mapping/*` rendering: speed-
 * bucketed trip polylines on a shared canvas renderer, balloon-pin event
 * markers in a markercluster group, start/end circle markers, and the pixel-
 * space route-disambiguation popup. The video player is a later lane, so a
 * disambiguation "pick" highlights the chosen trip rather than opening video.
 *
 * Parity-neutral SPA adaptations:
 *  - The OSM tile URL is read from `window.__TESLAUSB_TILE_URL__` (default OSM).
 *    UAT sets it to `""` to suppress ALL external tile fetches so the
 *    "every request same-origin, zero non-2xx" gate holds offline.
 *  - Test hooks (`window.__TESLAUSB_MAP__`, `window.__TESLAUSB_MAP_HOOKS__`)
 *    expose the live map + layer counts so Playwright can assert on real
 *    Leaflet state, not just DOM.
 */
import L from "leaflet";
import "leaflet.markercluster";
import { speedColor, type SpeedUnit } from "./speed";
import { makeEventIcon } from "./eventIcons";

const DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const DISAMBIG_PIXEL_RADIUS = 22;

/** A single route waypoint in canonical units (speed in m/s). */
export interface MapWaypoint {
  lat: number;
  lon: number;
  speed: number;
}

/** A trip ready to render: pre-decoded geometry + display metadata. */
export interface MapTrip {
  id: number;
  /** epoch seconds */
  startTime: number;
  distanceMi: number;
  durationMin: number;
  /** Per-point geometry with speed (preferred render path). */
  waypoints: MapWaypoint[];
  /** Fallback geometry (segments of [lat,lon]) when `waypoints` is empty. */
  polyline: [number, number][][];
  startCoord: [number, number] | null;
  endCoord: [number, number] | null;
}

/** An event bubble on the route. */
export interface MapEvent {
  id: number;
  type: string;
  severity: number | null;
  tripId: number | null;
  lat: number;
  lon: number;
  description: string;
  /** epoch seconds */
  t: number;
  /** Playable clip id, when the event has video. Drives the popup deep-link. */
  clipId?: number | null;
}

export interface MapFilters {
  enabledTypes: Set<string>;
  minSeverity: number;
  minDistanceMi: number;
  limitToView: boolean;
}

interface RenderInput {
  trips: MapTrip[];
  events: MapEvent[];
  unit: SpeedUnit;
  filters: MapFilters;
}

interface Candidate {
  trip: MapTrip;
  distance: number;
}

interface MapHooks {
  /** Visible speed-bucket polylines currently drawn. */
  tripPolylineCount: number;
  /** Event markers currently in the cluster group. */
  eventMarkerCount: number;
  /** Number of trips rendered in the last pass. */
  tripCount: number;
  /** Active display unit. */
  unit: SpeedUnit;
  /** Whether a tile layer was added (false under offline UAT). */
  hasTileLayer: boolean;
  /** Current count of rendered `.marker-cluster` bubbles (for clustering UAT). */
  clusterCount: () => number;
  /** Build id baked into the bundle (wiring proof). */
  build: string;
  /** Test hook: run the real disambiguation logic at a coordinate (as if the
   *  user clicked the route there) and return the candidate count. Drives the
   *  same findCandidates → popup/highlight path the click handler uses. */
  triggerDisambig: (lat: number, lon: number) => number;
  /** Test hook: read the ACTUAL visible route polylines from the live Leaflet
   *  layer group (colour + decoded coords), skipping the invisible click
   *  targets. Reflects real rendered geometry, not a controller counter. */
  visibleRouteLayers: () => { color: string; coords: [number, number][] }[];
  /** Test hook: read the ACTUAL event-marker coordinates from the live cluster
   *  group (proves bubbles landed at the expected route coords). */
  eventLatLngs: () => [number, number][];
}

function fmtLocalTime(epochSec: number): string {
  try {
    const d = new Date(epochSec * 1000);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return "Invalid Date";
  }
}

function escapeHtml(value: string): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Distance from container point `p` to segment `a→b`, in pixel space. */
function pointSegDistPx(p: L.Point, a: L.Point, b: L.Point): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const lenSq = dx * dx + dy * dy;
  if (lenSq === 0) {
    const px = p.x - a.x;
    const py = p.y - a.y;
    return Math.sqrt(px * px + py * py);
  }
  let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / lenSq;
  if (t < 0) t = 0;
  else if (t > 1) t = 1;
  const projX = a.x + t * dx;
  const projY = a.y + t * dy;
  const px = p.x - projX;
  const py = p.y - projY;
  return Math.sqrt(px * px + py * py);
}

export class TripMapController {
  private map: L.Map;
  private readonly tripLayer: L.LayerGroup;
  private readonly eventCluster: L.MarkerClusterGroup;
  private readonly disambigHighlightLayer: L.LayerGroup;
  private readonly canvasRenderer: L.Canvas;
  private readonly hasTileLayer: boolean;

  private last: RenderInput | null = null;
  private visibleTrips: MapTrip[] = [];
  private moveendTimer: number | null = null;
  private disambigPopup: L.Popup | null = null;
  private hooks: MapHooks;

  constructor(container: HTMLElement) {
    const tileUrl = (window as unknown as { __TESLAUSB_TILE_URL__?: string })
      .__TESLAUSB_TILE_URL__;

    // `maxZoom` is set on the map itself (not just the tile layer) because
    // leaflet.markercluster requires a finite map max-zoom to build its cluster
    // grid. With offline tiles (UAT) there's no tile layer to supply it, so the
    // cluster group would throw "Map has no maxZoom specified" — set it here.
    this.map = L.map(container, { preferCanvas: true, maxZoom: 19 }).setView(
      [37.7749, -122.4194],
      10,
    );

    // Offline UAT passes `""` to disable tiles entirely (keeps every request
    // same-origin). Any other value (or undefined) uses that URL / the OSM
    // default. We never register the legacy tile-cache Service Worker.
    if (tileUrl === "") {
      this.hasTileLayer = false;
    } else {
      L.tileLayer(tileUrl || DEFAULT_TILE_URL, {
        maxZoom: 19,
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      }).addTo(this.map);
      this.hasTileLayer = true;
    }

    this.canvasRenderer = L.canvas({ padding: 0.2 });
    this.tripLayer = L.layerGroup().addTo(this.map);
    this.eventCluster = L.markerClusterGroup({
      maxClusterRadius: 40,
      spiderfyOnMaxZoom: true,
    }).addTo(this.map);
    this.disambigHighlightLayer = L.layerGroup().addTo(this.map);

    this.hooks = {
      tripPolylineCount: 0,
      eventMarkerCount: 0,
      tripCount: 0,
      unit: "mph",
      hasTileLayer: this.hasTileLayer,
      clusterCount: () =>
        container.querySelectorAll(".marker-cluster").length,
      build:
        (window as unknown as { __TESLAUSB_BUILD__?: string })
          .__TESLAUSB_BUILD__ ?? "dev",
      triggerDisambig: (lat: number, lon: number) => {
        const latlng = L.latLng(lat, lon);
        const candidates = this.findCandidatesNearClick(latlng);
        if (candidates.length >= 2) {
          this.showDisambigPopup(latlng, candidates);
        } else if (candidates.length === 1) {
          this.highlightTrip(candidates[0].trip);
        }
        return candidates.length;
      },
      visibleRouteLayers: () => {
        const out: { color: string; coords: [number, number][] }[] = [];
        this.tripLayer.eachLayer((layer) => {
          if (!(layer instanceof L.Polyline)) return;
          const opts = layer.options as L.PolylineOptions;
          if ((opts.opacity ?? 1) <= 0) return; // skip invisible click targets
          const lls = layer.getLatLngs() as L.LatLng[];
          out.push({
            color: String(opts.color ?? ""),
            coords: lls.map((p) => [p.lat, p.lng] as [number, number]),
          });
        });
        return out;
      },
      eventLatLngs: () => {
        const out: [number, number][] = [];
        this.eventCluster.eachLayer((layer) => {
          if (layer instanceof L.Marker) {
            const ll = layer.getLatLng();
            out.push([ll.lat, ll.lng]);
          }
        });
        return out;
      },
    };

    const win = window as unknown as {
      __TESLAUSB_MAP__?: L.Map;
      __TESLAUSB_MAP_HOOKS__?: MapHooks;
    };
    win.__TESLAUSB_MAP__ = this.map;
    win.__TESLAUSB_MAP_HOOKS__ = this.hooks;

    this.map.on("popupclose", () => {
      this.disambigHighlightLayer.clearLayers();
    });
    this.map.on("moveend", this.onMoveEnd);
  }

  /** Leaflet needs a size recalculation once its container is laid out. */
  invalidate() {
    this.map.invalidateSize();
  }

  /** Re-render with a new display unit (re-colours polylines + speed popups). */
  setUnit(unit: SpeedUnit) {
    if (this.last) this.render({ ...this.last, unit });
  }

  /** Draw a full day: trips, events, start/end markers; fit bounds. */
  render(input: RenderInput) {
    const normalized = this.normalizeRenderInput(input);
    this.last = normalized;
    this.hooks.unit = normalized.unit;
    this.renderCurrent(true);
  }

  private onMoveEnd = () => {
    if (!this.last?.filters.limitToView) return;
    if (this.moveendTimer != null) {
      window.clearTimeout(this.moveendTimer);
    }
    this.moveendTimer = window.setTimeout(() => {
      this.moveendTimer = null;
      if (!this.last?.filters.limitToView) return;
      this.renderCurrent(false);
    }, 80);
  };

  private renderCurrent(allowFitBounds: boolean) {
    const input = this.last;
    if (!input) return;
    // The visible set is being rebuilt: any open disambiguation popup holds row
    // handlers that close over the PREVIOUS candidates, so a trip just hidden by
    // a filter/bbox change could still be picked from the stale popup. Close it.
    if (this.disambigPopup) {
      this.map.closePopup(this.disambigPopup);
      this.disambigPopup = null;
    }
    this.tripLayer.clearLayers();
    this.eventCluster.clearLayers();
    this.disambigHighlightLayer.clearLayers();

    let polylineCount = 0;
    const bounds: L.LatLngTuple[] = [];
    const mapBounds = input.filters.limitToView ? this.map.getBounds() : null;
    const visibleTrips = this.filterTrips(input.trips, input.filters, mapBounds);
    const visibleTripIds = new Set(visibleTrips.map((trip) => trip.id));
    const visibleEvents = this.filterEvents(
      input.events,
      input.filters,
      mapBounds,
      visibleTripIds,
    );
    this.visibleTrips = visibleTrips;

    for (const trip of visibleTrips) {
      polylineCount += this.renderTrip(trip, input.unit, bounds);
    }

    let eventMarkerCount = 0;
    for (const ev of visibleEvents) {
      if (!Number.isFinite(ev.lat) || !Number.isFinite(ev.lon)) continue;
      const marker = L.marker([ev.lat, ev.lon], {
        icon: makeEventIcon(ev.type),
      });
      const safeType = escapeHtml(ev.type || "").replace(/_/g, " ");
      const safeDesc = escapeHtml(ev.description || "");
      const watchLink =
        ev.clipId != null
          ? `<br><a class="map-watch-link" href="/events?event=${ev.id}">▶ Watch video</a>`
          : "";
      marker.bindPopup(
        `<strong>${safeType}</strong><br>${fmtLocalTime(ev.t)}<br>${safeDesc}${watchLink}`,
      );
      this.eventCluster.addLayer(marker);
      bounds.push([ev.lat, ev.lon]);
      eventMarkerCount++;
    }
    this.hooks.tripPolylineCount = polylineCount;
    this.hooks.eventMarkerCount = eventMarkerCount;
    this.hooks.tripCount = visibleTrips.length;

    if (allowFitBounds && !input.filters.limitToView && bounds.length > 0) {
      this.map.fitBounds(bounds, { padding: [30, 30] });
    }
  }

  private normalizeRenderInput(input: RenderInput): RenderInput {
    return {
      trips: input.trips,
      events: input.events,
      unit: input.unit,
      filters: {
        enabledTypes: new Set(input.filters.enabledTypes),
        minSeverity: input.filters.minSeverity,
        minDistanceMi: input.filters.minDistanceMi,
        limitToView: input.filters.limitToView,
      },
    };
  }

  private filterTrips(
    trips: MapTrip[],
    filters: MapFilters,
    mapBounds: L.LatLngBounds | null,
  ): MapTrip[] {
    return trips.filter((trip) => {
      const distance = Number.isFinite(trip.distanceMi) ? trip.distanceMi : 0;
      if (distance < filters.minDistanceMi) return false;
      if (!filters.limitToView || !mapBounds) return true;
      const tripBounds = this.tripBounds(trip);
      return tripBounds ? tripBounds.intersects(mapBounds) : false;
    });
  }

  private filterEvents(
    events: MapEvent[],
    filters: MapFilters,
    mapBounds: L.LatLngBounds | null,
    visibleTripIds: Set<number>,
  ): MapEvent[] {
    return events.filter((ev) => {
      if (!filters.enabledTypes.has(ev.type)) return false;
      if (
        filters.minSeverity > 0 &&
        (ev.severity == null || ev.severity < filters.minSeverity)
      ) {
        return false;
      }
      if (
        ev.tripId != null &&
        !visibleTripIds.has(ev.tripId)
      ) {
        return false;
      }
      if (!filters.limitToView || !mapBounds) return true;
      if (!Number.isFinite(ev.lat) || !Number.isFinite(ev.lon)) return false;
      return mapBounds.contains([ev.lat, ev.lon]);
    });
  }

  private tripBounds(trip: MapTrip): L.LatLngBounds | null {
    const points = trip.waypoints
      .filter((wp) => Number.isFinite(wp.lat) && Number.isFinite(wp.lon))
      .map((wp) => [wp.lat, wp.lon] as [number, number]);
    if (points.length) return L.latLngBounds(points as L.LatLngExpression[]);

    const polyPoints = trip.polyline
      .flat()
      .filter(([lat, lon]) => Number.isFinite(lat) && Number.isFinite(lon));
    if (!polyPoints.length) return null;
    return L.latLngBounds(polyPoints as L.LatLngExpression[]);
  }

  /** Render one trip; returns the number of visible polylines drawn. */
  private renderTrip(
    trip: MapTrip,
    unit: SpeedUnit,
    bounds: L.LatLngTuple[],
  ): number {
    const valid = trip.waypoints.filter(
      (wp) => Number.isFinite(wp.lat) && Number.isFinite(wp.lon),
    );

    // Fallback: no per-point geometry → draw the pre-decoded polyline segments
    // single-colour (we have no per-point speed in this path).
    if (valid.length < 2) {
      let count = 0;
      for (const seg of trip.polyline) {
        if (seg.length < 2) continue;
        L.polyline(seg as L.LatLngExpression[], {
          renderer: this.canvasRenderer,
          color: "#3b528b",
          weight: 4,
          opacity: 0.9,
          interactive: false,
        }).addTo(this.tripLayer);
        for (const c of seg) bounds.push(c);
        count++;
      }
      this.addEndpointMarkers(trip, bounds);
      return count;
    }

    if (trip.startCoord) bounds.push(trip.startCoord);
    if (trip.endCoord) bounds.push(trip.endCoord);

    // Speed-bucketed runs: emit one polyline per adjacent same-bucket run,
    // seeding each new run with the previous endpoint so buckets visually join.
    const segments: { color: string; latlngs: [number, number][] }[] = [];
    let currentColor: string | null = null;
    let currentRun: [number, number][] | null = null;
    for (let i = 0; i < valid.length; i++) {
      const wp = valid[i];
      const color = speedColor(wp.speed, unit);
      if (color !== currentColor) {
        if (currentRun && currentRun.length >= 2) {
          segments.push({ color: currentColor as string, latlngs: currentRun });
        }
        const seed: [number, number][] =
          currentRun && currentRun.length
            ? [currentRun[currentRun.length - 1]]
            : [];
        currentRun = seed.concat([[wp.lat, wp.lon]]);
        currentColor = color;
      } else if (currentRun) {
        currentRun.push([wp.lat, wp.lon]);
      }
    }
    if (currentRun && currentRun.length >= 2) {
      segments.push({ color: currentColor as string, latlngs: currentRun });
    }

    // Invisible wide click target for route disambiguation.
    const clickLatLngs = valid.map((wp) => [wp.lat, wp.lon]) as [
      number,
      number,
    ][];
    const clickTarget = L.polyline(clickLatLngs as L.LatLngExpression[], {
      renderer: this.canvasRenderer,
      color: "#000",
      weight: 14,
      opacity: 0,
      interactive: true,
    }).addTo(this.tripLayer);
    clickTarget.on("click", (e: L.LeafletMouseEvent) => {
      const candidates = this.findCandidatesNearClick(e.latlng);
      if (candidates.length >= 2) {
        this.showDisambigPopup(e.latlng, candidates);
      } else if (candidates.length === 1) {
        this.highlightTrip(candidates[0].trip);
      }
    });

    let count = 0;
    for (const seg of segments) {
      L.polyline(seg.latlngs as L.LatLngExpression[], {
        renderer: this.canvasRenderer,
        color: seg.color,
        weight: 4,
        opacity: 0.9,
        interactive: false,
      }).addTo(this.tripLayer);
      count++;
    }

    this.addEndpointMarkers(trip, bounds);
    return count;
  }

  private addEndpointMarkers(trip: MapTrip, bounds: L.LatLngTuple[]) {
    const wps = trip.waypoints.filter(
      (wp) => Number.isFinite(wp.lat) && Number.isFinite(wp.lon),
    );
    const first = wps.length ? ([wps[0].lat, wps[0].lon] as [number, number]) : null;
    const last = wps.length
      ? ([wps[wps.length - 1].lat, wps[wps.length - 1].lon] as [number, number])
      : null;
    const startCoord = trip.startCoord ?? first ?? polyEnd(trip, "start");
    const endCoord = trip.endCoord ?? last ?? polyEnd(trip, "end");
    if (!startCoord || !endCoord) return;

    L.circleMarker(startCoord, {
      radius: 7,
      fillColor: "#28a745",
      color: "#fff",
      weight: 2,
      fillOpacity: 0.9,
    })
      .bindPopup(
        `<strong>Trip #${trip.id}</strong><br>${fmtLocalTime(trip.startTime)}<br>` +
          `${trip.distanceMi.toFixed(1)} mi \u00B7 ${trip.durationMin} min`,
      )
      .addTo(this.tripLayer);
    bounds.push(startCoord);

    L.circleMarker(endCoord, {
      radius: 7,
      fillColor: "#dc3545",
      color: "#fff",
      weight: 2,
      fillOpacity: 0.9,
    })
      .bindPopup("Trip End")
      .addTo(this.tripLayer);
    bounds.push(endCoord);
  }

  // ── Route disambiguation (pixel-space, port of route_disambiguation.js) ──

  private findCandidatesNearClick(clickLatLng: L.LatLng): Candidate[] {
    const trips = this.visibleTrips;
    if (!trips.length) return [];
    const clickPt = this.map.latLngToContainerPoint(clickLatLng);
    const radius = DISAMBIG_PIXEL_RADIUS;
    const candidates: Candidate[] = [];

    for (const trip of trips) {
      const wps = trip.waypoints.filter(
        (wp) => Number.isFinite(wp.lat) && Number.isFinite(wp.lon),
      );
      if (wps.length < 1) continue;

      const projected: L.Point[] = wps.map((wp) =>
        this.map.latLngToContainerPoint([wp.lat, wp.lon]),
      );
      let minLeft = Infinity,
        minTop = Infinity,
        maxLeft = -Infinity,
        maxTop = -Infinity;
      for (const pt of projected) {
        if (pt.x < minLeft) minLeft = pt.x;
        if (pt.x > maxLeft) maxLeft = pt.x;
        if (pt.y < minTop) minTop = pt.y;
        if (pt.y > maxTop) maxTop = pt.y;
      }
      if (
        clickPt.x < minLeft - radius ||
        clickPt.x > maxLeft + radius ||
        clickPt.y < minTop - radius ||
        clickPt.y > maxTop + radius
      ) {
        continue;
      }

      let bestDist = Infinity;
      for (let i = 0; i < projected.length - 1; i++) {
        const d = pointSegDistPx(clickPt, projected[i], projected[i + 1]);
        if (d < bestDist) bestDist = d;
      }
      if (projected.length === 1) {
        const a = projected[0];
        bestDist = Math.sqrt(
          (clickPt.x - a.x) * (clickPt.x - a.x) +
            (clickPt.y - a.y) * (clickPt.y - a.y),
        );
      }
      if (bestDist <= radius) candidates.push({ trip, distance: bestDist });
    }

    candidates.sort((a, b) => {
      const ta = a.trip.startTime || 0;
      const tb = b.trip.startTime || 0;
      if (ta !== tb) return tb - ta;
      return a.distance - b.distance;
    });
    return candidates;
  }

  private highlightTrip(trip: MapTrip) {
    // Never highlight a trip that filtering has removed from the visible set
    // (defends against a stale popup row whose closure outlived a refilter).
    if (!this.visibleTrips.some((t) => t.id === trip.id)) return;
    this.disambigHighlightLayer.clearLayers();
    const latLngs = trip.waypoints
      .filter((wp) => Number.isFinite(wp.lat) && Number.isFinite(wp.lon))
      .map((wp) => [wp.lat, wp.lon]) as [number, number][];
    if (latLngs.length < 2) return;
    L.polyline(latLngs as L.LatLngExpression[], {
      renderer: this.canvasRenderer,
      color: "#3B82F6",
      weight: 6,
      opacity: 0.9,
      interactive: false,
    }).addTo(this.disambigHighlightLayer);
  }

  private showDisambigPopup(latlng: L.LatLng, candidates: Candidate[]) {
    const container = document.createElement("div");

    const header = document.createElement("div");
    header.className = "disambig-header";
    header.textContent = candidates.length + " clips through here";
    container.appendChild(header);

    const list = document.createElement("div");
    list.className = "disambig-list";

    for (const c of candidates) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "disambig-row";

      const main = document.createElement("div");
      main.className = "disambig-row-main";
      const primary = document.createElement("div");
      primary.className = "disambig-row-primary";
      primary.textContent = fmtLocalTime(c.trip.startTime);
      main.appendChild(primary);

      const secondary = document.createElement("div");
      secondary.className = "disambig-row-secondary";
      secondary.textContent =
        `${c.trip.distanceMi.toFixed(1)} mi \u00B7 ${c.trip.durationMin} min`;
      main.appendChild(secondary);
      row.appendChild(main);

      const chevron = document.createElement("span");
      chevron.className = "disambig-row-chevron";
      chevron.setAttribute("aria-hidden", "true");
      chevron.textContent = "\u203A";
      row.appendChild(chevron);

      row.addEventListener("mouseenter", () => this.highlightTrip(c.trip));
      row.addEventListener("focus", () => this.highlightTrip(c.trip));
      row.addEventListener("click", (ev) => {
        L.DomEvent.stopPropagation(ev);
        // Video player is a later lane: a pick highlights the chosen trip.
        this.highlightTrip(c.trip);
      });
      list.appendChild(row);
    }
    container.appendChild(list);

    const popup = L.popup({
      className: "disambig-popup",
      closeOnClick: true,
      autoClose: true,
      closeButton: true,
      keepInView: true,
      offset: L.point(0, -4),
    })
      .setLatLng(latlng)
      .setContent(container);
    this.disambigPopup = popup;
    popup.once("remove", () => {
      if (this.disambigPopup === popup) this.disambigPopup = null;
    });
    popup.openOn(this.map);
  }

  /** Tear down the map + null the global test hooks. Idempotent. */
  destroy() {
    this.map.off("moveend", this.onMoveEnd);
    if (this.moveendTimer != null) {
      window.clearTimeout(this.moveendTimer);
      this.moveendTimer = null;
    }
    const win = window as unknown as {
      __TESLAUSB_MAP__?: L.Map;
      __TESLAUSB_MAP_HOOKS__?: MapHooks;
    };
    win.__TESLAUSB_MAP__ = undefined;
    win.__TESLAUSB_MAP_HOOKS__ = undefined;
    this.map.remove();
    this.last = null;
    this.visibleTrips = [];
    this.disambigPopup = null;
  }
}

/** Endpoint coord from the fallback polyline geometry (first/last vertex). */
function polyEnd(trip: MapTrip, which: "start" | "end"): [number, number] | null {
  const segs = trip.polyline;
  if (!segs.length) return null;
  if (which === "start") {
    const first = segs[0];
    return first.length ? first[0] : null;
  }
  const lastSeg = segs[segs.length - 1];
  return lastSeg.length ? lastSeg[lastSeg.length - 1] : null;
}
