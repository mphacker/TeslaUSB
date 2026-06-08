import { useEffect, useRef, useState } from "preact/hooks";
import { Icon } from "../components/Icon";
import { api, ApiError } from "../api/client";
import type { Clip, EventItem } from "../api/types";
import { HudController, type HudElements } from "../player/hud-controller";
import "../styles/player.css";

/**
 * The event-player screen (route `/events`, Shell active "map") — visual +
 * structural parity with the legacy Flask `event_player.html`: a fullscreen
 * immersive Tesla-cam player (native `<video>` over webd's byte-range stream)
 * with the camera selector, the SEI/HUD overlay toggle, and the telemetry HUD
 * drawn over the video.
 *
 * Data comes only from webd's read-only catalog + media API:
 *  - playlist → `/api/events` (events that carry a `clip_id`)
 *  - angles   → `/api/clips/:id` (the clip's available camera angles)
 *  - video    → `<video src=/api/clips/:id/stream?camera=>` (browser range reqs)
 *  - download → `/api/clips/:id/export` (ZIP of the clip's archive angles)
 *
 * The Tesla HUD is a non-Preact, per-frame concern, so it is driven imperatively
 * by {@link HudController} via a ref/effect — the same "imperative lib behind a
 * ref" pattern as `map/controller.ts`. The controller reads telemetry from the
 * streamed MP4's embedded SEI in production, or from a UAT-seeded fixture.
 *
 * DEFERRED (webd 5.1c, routed through the unbuilt gadgetd eject hand-off): the
 * delete-clip and archive-to-cloud mutations. Both render inert/disabled here,
 * exactly as the media-hub did for its deferred mutation forms.
 *
 * FLAG (nav placement): there is no "events" NavKey, so this screen highlights
 * "map" — the existing reversible router default. webd also exposes no city for
 * an event, so the location heading shows the event description (the most
 * place-like text available), not a reverse-geocoded city as the legacy did.
 */

interface CameraDef {
  /** webd angle camera name (the `?camera=` value + DB `angles.camera`). */
  key: string;
  label: string;
  icon: string;
}

const CAMERAS: CameraDef[] = [
  { key: "front", label: "Front", icon: "arrow-up" },
  { key: "back", label: "Rear", icon: "arrow-down" },
  { key: "left_repeater", label: "Left", icon: "arrow-left" },
  { key: "right_repeater", label: "Right", icon: "arrow-right" },
];

/** Pillar cameras the dashcam never records — always shown unavailable. */
const PILLARS = [
  { label: "Left Pillar", icon: "chevrons-left" },
  { label: "Right Pillar", icon: "chevrons-right" },
];

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Format an epoch-second event time as the legacy "YYYY-MM-DD hh:mm:ss AM/PM". */
function fmtDateTime(epochSec: number): string {
  const d = new Date(epochSec * 1000);
  let h = d.getHours();
  const ampm = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(h)}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())} ${ampm}`
  );
}

/** Humanise an event `type` enum into a Title-Case label. */
function humanize(s: string): string {
  return s
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Heading text for an event (webd has no city; description is the best proxy). */
function locationLabel(ev: EventItem | undefined): string {
  if (!ev) return "\u2014";
  if (ev.description) return ev.description;
  if (ev.lat != null && ev.lon != null)
    return `${ev.lat.toFixed(4)}, ${ev.lon.toFixed(4)}`;
  return humanize(ev.type);
}

/** Total on-disk size of a clip's angles, formatted as "X.XX MB". */
function clipSize(clip: Clip | null): string {
  if (!clip) return "\u2014";
  const bytes = clip.angles.reduce((n, a) => n + (a.size_bytes ?? 0), 0);
  return `${(bytes / 1_000_000).toFixed(2)} MB`;
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : (err as Error).message;
}

export function EventPlayer() {
  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const ctrlRef = useRef<HudController | null>(null);

  const [events, setEvents] = useState<EventItem[] | null>(null);
  const [index, setIndex] = useState(0);
  const [clip, setClip] = useState<Clip | null>(null);
  const [camera, setCamera] = useState("front");
  const [hudOn, setHudOn] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentEvent = events && events.length ? events[index] : undefined;
  const streamUrl = clip ? api.streamUrl(clip.id, camera) : "";

  // ── Mount: seed HUD toggle from localStorage + load the event playlist. ──
  useEffect(() => {
    try {
      setHudOn(localStorage.getItem("seiOverlayEnabled") === "true");
    } catch {
      /* localStorage may be unavailable; default off */
    }
    const ac = new AbortController();
    (async () => {
      try {
        const page = await api.events({ limit: 100 }, ac.signal);
        // The player only lists events that have a playable clip.
        const playable = page.items.filter((e) => e.clip_id != null);
        setEvents(playable);
        setIndex(0);
      } catch (err) {
        if (ac.signal.aborted) return;
        setError(errMessage(err));
      }
    })();
    return () => ac.abort();
  }, []);

  // ── Create the imperative HUD controller once the DOM is mounted. ──
  useEffect(() => {
    const video = videoRef.current;
    const container = containerRef.current;
    if (!video || !container) return;
    const q = (sel: string) => container.querySelector(sel) as HTMLElement;
    const hud: HudElements = {
      gear: q("#hudGear"),
      speed: q("#hudSpeed"),
      steering: q("#hudSteering"),
      brakePedal: q("#brakePedal"),
      throttlePedal: q("#throttlePedal"),
      blinkerLeft: q("#blinkerLeft"),
      blinkerRight: q("#blinkerRight"),
      autopilot: q("#autopilotIndicator"),
    };
    const ctrl = new HudController(video, hud);
    ctrlRef.current = ctrl;
    return () => {
      ctrl.destroy();
      ctrlRef.current = null;
    };
  }, []);

  // ── Resolve the current event's clip (angles) whenever it changes. ──
  useEffect(() => {
    if (!currentEvent || currentEvent.clip_id == null) return;
    const ac = new AbortController();
    (async () => {
      try {
        const c = await api.clip(currentEvent.clip_id as number, ac.signal);
        setClip(c);
        setCamera("front");
      } catch (err) {
        if (ac.signal.aborted) return;
        setError(errMessage(err));
      }
    })();
    return () => ac.abort();
  }, [currentEvent?.id]);

  // ── (Re)load HUD telemetry when the streamed clip/camera changes, but only
  //    while the overlay is on (matches the legacy "load SEI on toggle" path
  //    and avoids fetching the whole MP4 when the HUD is hidden). ──
  useEffect(() => {
    const ctrl = ctrlRef.current;
    if (!ctrl || !streamUrl || !hudOn) return;
    void ctrl.loadTelemetry(streamUrl);
  }, [streamUrl, hudOn]);

  const onToggleHud = (e: Event) => {
    const on = (e.target as HTMLInputElement).checked;
    setHudOn(on);
    try {
      localStorage.setItem("seiOverlayEnabled", String(on));
    } catch {
      /* persistence is best-effort */
    }
  };

  const switchCamera = (cam: CameraDef) => {
    if (!clip) return;
    if (!clip.angles.some((a) => a.camera === cam.key)) return;
    if (cam.key === camera) return;
    setCamera(cam.key);
  };

  const cameraAvailable = (cam: CameraDef): boolean =>
    !!clip && clip.angles.some((a) => a.camera === cam.key);

  return (
    <div class="event-player-container" data-screen="event-player" ref={containerRef}>
      {/* Back button overlay */}
      <a href="/" class="back-link" aria-label="Close player">
        <Icon name="x" />
      </a>

      {/* Main video */}
      <div class="main-video-container">
        <video
          id="mainVideo"
          class="main-video"
          controls
          playsInline
          ref={videoRef}
          src={streamUrl || undefined}
          data-original-url={streamUrl || undefined}
        >
          Your browser does not support the video tag.
        </video>
      </div>

      {/* Top overlay with location and info */}
      <div class="event-header" id="topOverlay">
        <div class="event-info">
          <h2 class="event-location">{locationLabel(currentEvent)}</h2>
          <div class="event-datetime">
            {currentEvent ? fmtDateTime(currentEvent.t) : "\u2014"}
          </div>
        </div>

        {/* Tesla HUD with SEI data */}
        <div class={`tesla-hud${hudOn ? "" : " hidden"}`} id="teslaHud">
          <div class="hud-card">
            <div class="hud-grid">
              <div class="hud-gear" id="hudGear">P</div>

              <div class="hud-pedal brake" id="brakePedal" style="--pedal-fill: 0%;">
                <span class="fill">
                  <i />
                </span>
                <svg viewBox="0 0 24 24" width="24" height="24">
                  <path d="M6 7 L18 7 L20 16 Q12 19 4 16 Z" stroke-width="2" stroke-linejoin="round" />
                  <line x1="8" y1="9" x2="8" y2="14" stroke-width="1.5" />
                  <line x1="10" y1="9" x2="10" y2="14" stroke-width="1.5" />
                  <line x1="12" y1="9" x2="12" y2="14" stroke-width="1.5" />
                  <line x1="14" y1="9" x2="14" y2="14" stroke-width="1.5" />
                  <line x1="16" y1="9" x2="16" y2="14" stroke-width="1.5" />
                </svg>
              </div>

              <span class="hud-blinker left" id="blinkerLeft">
                {"\u25C4"}
              </span>

              <div class="hud-speed">
                <div class="hud-speed-value" id="hudSpeed">0</div>
                <div class="hud-speed-label">mph</div>
              </div>

              <span class="hud-blinker right" id="blinkerRight">
                {"\u25BA"}
              </span>

              <div class="hud-steering" id="hudSteering" style="--wheel-rotation: 0deg;">
                <svg viewBox="0 0 24 24" fill="none">
                  <circle cx="12" cy="12" r="8" stroke="white" stroke-width="1.4" />
                  <path d="M6.8 9.8 H17.2" stroke="white" stroke-width="2" stroke-linecap="round" />
                  <path d="M12 9.8 V16.8" stroke="white" stroke-width="2" stroke-linecap="round" />
                  <circle cx="12" cy="12" r="1.8" stroke="white" stroke-width="1.4" />
                </svg>
              </div>

              <div class="hud-pedal throttle" id="throttlePedal" style="--pedal-fill: 0%;">
                <span class="fill">
                  <i />
                </span>
                <svg viewBox="0 0 24 24" width="24" height="24">
                  <path d="M9 4 L15 4 L16 18 Q12 20 8 18 Z" stroke-width="2" stroke-linejoin="round" />
                  <rect x="9" y="2" width="6" height="2" rx="1" stroke-width="2" />
                </svg>
              </div>

              <div class="hud-autopilot" id="autopilotIndicator" />
            </div>
          </div>
        </div>

        <div class="event-meta-right">
          <div>{currentEvent ? humanize(currentEvent.type) : "\u2014"}</div>
          <div>{clipSize(clip)}</div>
        </div>
      </div>

      {/* Bottom camera selector with Tesla-style layout */}
      <div class="camera-selector">
        {/* SEI/HUD overlay toggle */}
        <div class="sei-toggle-container">
          <div class="sei-toggle-label">
            HUD
            <br />
            Overlay
          </div>
          <label class="sei-toggle-switch">
            <input
              type="checkbox"
              id="seiToggle"
              checked={hudOn}
              onChange={onToggleHud}
            />
            <span class="sei-toggle-slider" />
          </label>
        </div>

        {CAMERAS.map((cam) => {
          const available = cameraAvailable(cam);
          const active = available && cam.key === camera;
          return (
            <div
              key={cam.key}
              class={`camera-option${active ? " active" : ""}${available ? "" : " unavailable"}`}
              data-camera={cam.key}
              onClick={() => switchCamera(cam)}
              role="button"
              aria-disabled={available ? "false" : "true"}
            >
              <Icon name={cam.icon} class="camera-icon" />
              <div class="camera-label">{cam.label}</div>
            </div>
          );
        })}

        {PILLARS.map((p) => (
          <div class="camera-option unavailable" key={p.label}>
            <Icon name={p.icon} class="camera-icon" />
            <div class="camera-label">{p.label}</div>
          </div>
        ))}

        {/* Download all angles (ZIP export) */}
        <a
          class={`camera-option download-option${clip ? "" : " disabled"}`}
          id="downloadButton"
          href={clip ? api.exportUrl(clip.id) : undefined}
          download
          aria-disabled={clip ? "false" : "true"}
        >
          <Icon name="download" class="camera-icon" />
          <div class="camera-label">Download All</div>
        </a>

        {/* Archive to cloud — DEFERRED (webd 5.1c): inert. */}
        <div
          class="camera-option archive-option disabled"
          id="archiveButton"
          title="Archiving is deferred to webd 5.1c"
          aria-disabled="true"
        >
          <Icon name="cloud-upload" class="camera-icon" />
          <div class="camera-label">Archive</div>
        </div>

        {/* Delete clip — DEFERRED (webd 5.1c, gadgetd eject hand-off): inert. */}
        <div
          class="camera-option delete-option disabled"
          id="deleteButton"
          title="Delete is deferred to webd 5.1c"
          aria-disabled="true"
        >
          <Icon name="trash-2" class="camera-icon" />
          <div class="camera-label">Delete</div>
        </div>
      </div>

      {error && (
        <div
          style="position:absolute;bottom:110px;left:50%;transform:translateX(-50%);color:#fff;background:rgba(120,30,30,0.85);padding:8px 14px;border-radius:8px;z-index:30;font-size:0.85em;"
          role="alert"
        >
          {error}
        </div>
      )}
    </div>
  );
}
