/**
 * Event-marker balloon-pin icons — a faithful port of the legacy
 * `static/js/mapping/route_disambiguation.js` `eventMarkerSvgs` table and
 * `day_navigation.js` `makeEventIcon`. Each marker is a circular coloured
 * bubble with a white skeuomorphic glyph and a pointer tail, built as a Leaflet
 * `divIcon` (className `event-svg-icon`, 34×44, anchored at the tail tip).
 *
 * The honk glyph is included so the bubble lights up the moment indexd starts
 * ingesting honk/event.json (a tracked Phase-5 parity gap — honk events are not
 * populated yet, so the UAT cannot assert them).
 */
import L from "leaflet";

interface EventSvg {
  color: string;
  icon: string;
}

export const eventMarkerSvgs: Record<string, EventSvg> = {
  harsh_braking: {
    color: "#dc3545",
    icon:
      '<rect x="11" y="6" width="10" height="5" rx="1.5" fill="#fff"/>' +
      '<rect x="14" y="11" width="4" height="7" rx="1" fill="#fff"/>' +
      '<line x1="12" y1="20" x2="20" y2="20" stroke="#fff" stroke-width="2" stroke-linecap="round"/>',
  },
  emergency_braking: {
    color: "#b91c1c",
    icon:
      '<rect x="11" y="6" width="10" height="5" rx="1.5" fill="#fff"/>' +
      '<rect x="14" y="11" width="4" height="5" rx="1" fill="#fff"/>' +
      '<line x1="12" y1="18" x2="20" y2="18" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' +
      '<line x1="16" y1="6.5" x2="16" y2="9" stroke="#b91c1c" stroke-width="2" stroke-linecap="round"/>' +
      '<circle cx="16" cy="10" r="0.8" fill="#b91c1c"/>',
  },
  hard_acceleration: {
    color: "#16a34a",
    icon:
      '<path d="M19,6 L15,6 L11,18 L14,18 L17,9 L19,9 Z" fill="#fff"/>' +
      '<line x1="11" y1="20" x2="21" y2="20" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' +
      '<circle cx="12" cy="18.5" r="1.5" fill="none" stroke="#fff" stroke-width="1.5"/>',
  },
  sharp_turn: {
    color: "#f59e0b",
    icon:
      '<g transform="rotate(-30,16,13)">' +
      '<circle cx="16" cy="13" r="7.5" fill="none" stroke="#fff" stroke-width="2.5"/>' +
      '<circle cx="16" cy="13" r="2" fill="#fff"/>' +
      '<line x1="16" y1="5.5" x2="16" y2="11" stroke="#fff" stroke-width="2.2"/>' +
      '<line x1="9.5" y1="16.5" x2="14.2" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
      '<line x1="22.5" y1="16.5" x2="17.8" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
      "</g>",
  },
  autopilot_engaged: {
    color: "#3b82f6",
    icon:
      '<circle cx="16" cy="13" r="7.5" fill="none" stroke="#fff" stroke-width="2.5"/>' +
      '<circle cx="16" cy="13" r="2" fill="#fff"/>' +
      '<line x1="16" y1="5.5" x2="16" y2="11" stroke="#fff" stroke-width="2.2"/>' +
      '<line x1="9.5" y1="16.5" x2="14.2" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
      '<line x1="22.5" y1="16.5" x2="17.8" y2="14.3" stroke="#fff" stroke-width="2.2"/>' +
      '<circle cx="22" cy="7" r="4" fill="#fff"/>' +
      '<text x="22" y="9.5" text-anchor="middle" font-size="7" font-weight="bold" fill="#3b82f6" font-family="sans-serif">A</text>',
  },
  autopilot_disengaged: {
    color: "#f97316",
    icon:
      '<circle cx="16" cy="14" r="7" fill="none" stroke="#fff" stroke-width="2.2"/>' +
      '<line x1="16" y1="7" x2="16" y2="11.5" stroke="#fff" stroke-width="2"/>' +
      '<line x1="10" y1="17.5" x2="14" y2="15.5" stroke="#fff" stroke-width="2"/>' +
      '<line x1="22" y1="17.5" x2="18" y2="15.5" stroke="#fff" stroke-width="2"/>' +
      '<path d="M11.5,18 Q11.5,21 16,21 Q20.5,21 20.5,18" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>',
  },
  speed_limit_exceeded: {
    color: "#ec4899",
    icon:
      '<path d="M7,18 A9,9 0 1,1 25,18" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/>' +
      '<line x1="16" y1="16" x2="21" y2="9" stroke="#fff" stroke-width="3" stroke-linecap="round"/>' +
      '<circle cx="16" cy="16" r="2" fill="#fff"/>' +
      '<line x1="9" y1="10" x2="10.5" y2="11.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>' +
      '<line x1="16" y1="6.5" x2="16" y2="8.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>' +
      '<line x1="23" y1="10" x2="21.5" y2="11.5" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/>',
  },
  sentry: {
    color: "#8b5cf6",
    icon:
      '<path d="M7,13 Q16,4 25,13 Q16,22 7,13 Z" fill="none" stroke="#fff" stroke-width="2.2"/>' +
      '<circle cx="16" cy="13" r="3.5" fill="#fff"/>' +
      '<circle cx="16" cy="13" r="1.8" fill="#8b5cf6"/>',
  },
  saved: {
    color: "#007bff",
    icon:
      '<path d="M10,6 L22,6 L22,21 L16,17.5 L10,21 Z" fill="none" stroke="#fff" stroke-width="2.5" stroke-linejoin="round"/>',
  },
  // Honk — speaker + sound-wave arcs. Not yet emitted by indexd (Phase-5 parity
  // gap); included so the bubble renders the instant honk ingestion lands.
  honk: {
    color: "#0ea5e9",
    icon:
      '<path d="M10,11 L13,11 L17,8 L17,18 L13,15 L10,15 Z" fill="#fff"/>' +
      '<path d="M20,9 Q23,13 20,17" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"/>' +
      '<path d="M22,7 Q26,13 22,19" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>',
  },
};

/** The fallback grey bubble used for any unrecognised event type. */
const FALLBACK: EventSvg = {
  color: "#6c757d",
  icon: '<circle cx="16" cy="13" r="5" fill="#fff"/>',
};

/** Per-type accent colour (used for the filter pills + marker bubble). */
export function eventColor(eventType: string): string {
  return (eventMarkerSvgs[eventType] ?? FALLBACK).color;
}

/** Build the Leaflet `divIcon` balloon-pin for an event type (port of legacy). */
export function makeEventIcon(eventType: string): L.DivIcon {
  const cfg = eventMarkerSvgs[eventType] ?? FALLBACK;
  const id = "ds_" + eventType;
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 42" width="34" height="44">` +
    `<defs><filter id="${id}"><feDropShadow dx="0" dy="1.5" stdDeviation="1.5" flood-opacity=".3"/></filter></defs>` +
    `<g filter="url(#${id})">` +
    `<circle cx="16" cy="14.5" r="13.5" fill="${cfg.color}" stroke="#fff" stroke-width="2"/>` +
    `<polygon points="11,26 16,37 21,26" fill="${cfg.color}" stroke="#fff" stroke-width="2" stroke-linejoin="round"/>` +
    `</g>` +
    `${cfg.icon}` +
    `</svg>`;
  return L.divIcon({
    html: svg,
    className: "event-svg-icon",
    iconSize: [34, 44],
    iconAnchor: [17, 43],
    popupAnchor: [0, -40],
  });
}
