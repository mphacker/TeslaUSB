import { useEffect, useRef, useState } from "preact/hooks";
import { Icon } from "../components/Icon";
import { api, ApiError } from "../api/client";
import type { Angle, Clip, EventItem } from "../api/types";
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
 *  - angle dl → `/api/clips/:id/angles/:camera/download` (single archive MP4)
 *
 * The Tesla HUD is a non-Preact, per-frame concern, so it is driven imperatively
 * by {@link HudController} via a ref/effect — the same "imperative lib behind a
 * ref" pattern as `map/controller.ts`. The controller reads telemetry from the
 * streamed MP4's embedded SEI in production, or from a UAT-seeded fixture.
 *
 * DEFERRED (webd 5.1c): the archive-to-cloud mutation renders inert/disabled
 * here, exactly as the media-hub did for its deferred mutation forms. The
 * delete-clip mutation IS wired (webd `DELETE /api/clips/:id?target=car`, the
 * `gadgetd` eject-handoff): an operator-gated confirm dialog issues a single
 * synchronous, terminal delete and reflects success/busy-retry/error inline.
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

/** The one `view_kind` webd's stream/export handlers actually serve: the
 *  Pi-side archive copy of a clip angle. MUST match webd `media.rs`
 *  (`VIEW_ARCHIVE`). */
const VIEW_ARCHIVE = "archive";

/** An angle is playable iff webd will stream it. Live `ro_usb` angles (a clip
 *  still on the car's USB, not yet archived) `404` on stream — raw exFAT
 *  byte-range streaming is a deferred seam — so we must not point a `<video>`
 *  at them. Any unknown kind is treated as not-yet-playable: a safe default
 *  that never fires a doomed request and respects `view_kind` being opaque. */
function isStreamableAngle(angle: Angle | undefined): boolean {
  return angle?.view_kind === VIEW_ARCHIVE;
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : (err as Error).message;
}

/** How the delete UI should react to a failed `deleteClip` call. */
interface DeleteFailure {
  message: string;
  /** Transient — keep the dialog open and offer a Retry button. */
  retryable: boolean;
  /** The clip is already gone from the car — treat as a soft success (remove it). */
  softGone: boolean;
}

/**
 * Map a `deleteClip` rejection to operator-facing UI state, keyed on the HTTP
 * `status` (and `code` where it changes the meaning). The contract's terminal
 * outcomes:
 *  - `409 handoff_busy` / network → transient, retryable.
 *  - `409 not_present` / `404`    → already gone from the car → soft success.
 *  - `503` (gadgetd unreachable)  → distinct message, retryable (may come back).
 *  - `400` / `422`                → validation, terminal (no retry).
 *  - `502` / `500` / `501`        → failed / fault / unsupported, terminal.
 */
function classifyDeleteFailure(err: unknown): DeleteFailure {
  if (err instanceof ApiError) {
    if (err.status === 0 || err.code === "network") {
      return {
        message: "Couldn't reach the device. Check the connection and retry.",
        retryable: true,
        softGone: false,
      };
    }
    if (err.status === 409) {
      if (err.code === "not_present") {
        return {
          message: "This clip is already gone from the car — removing it.",
          retryable: false,
          softGone: true,
        };
      }
      return {
        message: `${err.message} You can retry in a moment.`,
        retryable: true,
        softGone: false,
      };
    }
    if (err.status === 404) {
      return {
        message: "That clip no longer exists — removing it.",
        retryable: false,
        softGone: true,
      };
    }
    if (err.status === 503) {
      return {
        message: "The device is unreachable right now. Try again once it's back.",
        retryable: true,
        softGone: false,
      };
    }
    if (err.status === 400 || err.status === 422) {
      return { message: err.message, retryable: false, softGone: false };
    }
    if (err.status === 502) {
      return {
        message: `The delete couldn't be completed on the car: ${err.message}`,
        retryable: false,
        softGone: false,
      };
    }
    if (err.status === 500) {
      return {
        message: `The device reported a fault during delete: ${err.message}`,
        retryable: false,
        softGone: false,
      };
    }
    if (err.status === 501) {
      return {
        message: "Only car-side delete is available.",
        retryable: false,
        softGone: false,
      };
    }
    return { message: err.message, retryable: false, softGone: false };
  }
  return {
    message: (err as Error).message || "Unexpected error.",
    retryable: true,
    softGone: false,
  };
}

/** Seconds into the *currently selected camera's* video where the event moment
 *  falls. The event's `front_frame_offset_ms` is relative to the FRONT cam's
 *  own start; each angle starts at its `offset_ms` within the clip, so the
 *  event's clip-canonical position is `front.offset_ms + front_frame_offset_ms`
 *  and the seek target for any camera is that minus the camera's own
 *  `offset_ms`. Returns 0 when there's no offset to honor (start of clip). */
function eventSeekSeconds(
  clip: Clip | null,
  ev: EventItem | undefined,
  camera: string,
): number {
  if (!clip || !ev || ev.front_frame_offset_ms == null) return 0;
  const front = clip.angles.find((a) => a.camera === "front");
  const target = clip.angles.find((a) => a.camera === camera);
  if (!front || !target) return 0;
  const canonicalMs = front.offset_ms + ev.front_frame_offset_ms;
  return Math.max(0, (canonicalMs - target.offset_ms) / 1000);
}

interface DeepLink {
  eventId: number | null;
  clipId: number | null;
}

/** Parse deep-link params from `window.location.search`. */
function deepLink(): DeepLink {
  if (typeof window === "undefined") return { eventId: null, clipId: null };
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(window.location.search);
  } catch {
    return { eventId: null, clipId: null };
  }
  const rawEvent = params.get("event");
  const rawClip = params.get("clip");
  const eventId = rawEvent ? Number(rawEvent) : NaN;
  const clipId = rawClip ? Number(rawClip) : NaN;
  return {
    eventId: Number.isFinite(eventId) ? eventId : null,
    clipId: Number.isFinite(clipId) ? clipId : null,
  };
}

/** Resolve URL deep-link to either an event index or a direct clip id. */
function initialSelection(
  playable: EventItem[],
  { eventId, clipId }: DeepLink,
): { index: number; directClipId: number | null } {
  if (eventId != null) {
    const i = playable.findIndex((e) => e.id === eventId);
    if (i >= 0) return { index: i, directClipId: null };
  }
  if (clipId != null) {
    const i = playable.findIndex((e) => e.clip_id === clipId);
    if (i >= 0) return { index: i, directClipId: null };
    return { index: 0, directClipId: clipId };
  }
  return { index: 0, directClipId: null };
}

export function EventPlayer() {
  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const ctrlRef = useRef<HudController | null>(null);

  const [events, setEvents] = useState<EventItem[] | null>(null);
  const [index, setIndex] = useState(0);
  const [directClipId, setDirectClipId] = useState<number | null>(null);
  const [search, setSearch] = useState(
    () => (typeof window !== "undefined" ? window.location.search : ""),
  );
  const [clip, setClip] = useState<Clip | null>(null);
  const [camera, setCamera] = useState("front");
  const [hudOn, setHudOn] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ── Clip-delete (operator-gated destructive action) ──
  const [pending, setPending] = useState<{ clipId: number; label: string } | null>(
    null,
  );
  const [deleting, setDeleting] = useState(false);
  const [deleteFail, setDeleteFail] = useState<DeleteFailure | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const deleteAbortRef = useRef<AbortController | null>(null);

  const inDirectMode = directClipId != null;
  const currentEvent =
    !inDirectMode && events && events.length ? events[index] : undefined;
  const currentAngle = clip?.angles.find((a) => a.camera === camera);
  // Only build a stream URL for an angle webd will actually serve; pointing the
  // <video> at a non-archive (ro_usb) angle 404s and logs a console error.
  const streamUrl =
    clip && isStreamableAngle(currentAngle) ? api.streamUrl(clip.id, camera) : "";
  // A clip is playable when any angle is archive-backed. ro_usb-only clips are
  // still live on the car's USB and have nothing webd can stream or export yet.
  const clipPlayable = !!clip && clip.angles.some(isStreamableAngle);
  // The clip id the current selection points at (the event's clip, or the direct
  // clip). `clip` resolves asynchronously, so it can briefly lag the selection
  // right after a query change. `clipReady` means they're in sync; it gates the
  // destructive Delete action so a fast click can't delete the *old* clip while
  // the newly-selected one is still loading.
  const selectedClipId = currentEvent?.clip_id ?? directClipId;
  const clipReady = !!clip && clip.id === selectedClipId;
  const angleDownloadReady = clipReady && !!clip && isStreamableAngle(currentAngle);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const syncSearch = () => setSearch(window.location.search);
    const onPopState = () => syncSearch();
    const origPush = window.history.pushState;
    const patchedPushState: typeof window.history.pushState = function (
      this: History,
      ...args: Parameters<History["pushState"]>
    ): ReturnType<History["pushState"]> {
      const result = origPush.apply(this, args);
      syncSearch();
      return result;
    };
    window.addEventListener("popstate", onPopState);
    window.history.pushState = patchedPushState;
    return () => {
      window.removeEventListener("popstate", onPopState);
      // Only restore if our patch is still installed — guards against clobbering
      // a newer patch should two instances ever overlap.
      if (window.history.pushState === patchedPushState) {
        window.history.pushState = origPush;
      }
    };
  }, []);

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
        // The player only lists events that have a playable clip. The global
        // `/api/events` feed is newest-first (it backs the map side-panel's
        // descending catalog browser); the event player instead walks its
        // playlist chronologically (oldest -> newest), so sort here — decoupled
        // from the API's default order — to keep prev/next stable.
        const playable = page.items
          .filter((e) => e.clip_id != null)
          .sort((a, b) => a.t - b.t || a.id - b.id);
        // Resolve the deep-link selection atomically with the playlist so no
        // intermediate render can fall back to events[0] (which would flash the
        // wrong event's metadata and kick off a wasted clip fetch).
        const selection = initialSelection(playable, deepLink());
        setEvents(playable);
        setIndex(selection.index);
        setDirectClipId(selection.directClipId);
      } catch (err) {
        if (ac.signal.aborted) return;
        setError(errMessage(err));
      }
    })();
    return () => ac.abort();
  }, []);

  // Re-derive the selection when the URL QUERY changes while mounted (a
  // same-path ?clip/?event nav or back/forward — the shared router only tracks
  // pathname, so EventPlayer won't remount). Intentionally keyed on `search`
  // ONLY, never `events`: a playlist mutation (e.g. deleting the current clip)
  // must NOT re-run this, or an unchanged ?clip=<deleted-id> would resurrect the
  // just-removed clip as a direct clip. On mount it runs once with events=null
  // and returns — the fetch effect owns the atomic initial selection. When
  // `search` changes, React re-runs this with the latest render's `events`
  // closure, so the read below is current.
  useEffect(() => {
    if (!events) return;
    const sel = initialSelection(events, deepLink());
    setIndex(sel.index);
    setDirectClipId(sel.directClipId);
  }, [search]);

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
    const clipId = currentEvent?.clip_id ?? directClipId;
    if (clipId == null) {
      setClip(null);
      return;
    }
    const ac = new AbortController();
    (async () => {
      try {
        const c = await api.clip(clipId, ac.signal);
        setClip(c);
        setCamera("front");
        setError(null);
      } catch (err) {
        if (ac.signal.aborted) return;
        // Drop any previously-loaded clip so a failed re-resolve can't leave
        // stale video on screen under the new error/selection.
        setClip(null);
        setError(errMessage(err));
      }
    })();
    return () => ac.abort();
  }, [currentEvent?.id, directClipId]);

  // ── (Re)load HUD telemetry when the streamed clip/camera changes, but only
  //    while the overlay is on (matches the legacy "load SEI on toggle" path
  //    and avoids fetching the whole MP4 when the HUD is hidden). ──
  useEffect(() => {
    const ctrl = ctrlRef.current;
    if (!ctrl || !streamUrl || !hudOn) return;
    void ctrl.loadTelemetry(streamUrl);
  }, [streamUrl, hudOn]);

  // ── Seek to the event moment once the (re)loaded video has metadata. Without
  //    this the player always started at 0 and ignored front_frame_offset_ms,
  //    so events buried mid-clip never showed at the event. Keyed on streamUrl
  //    (which changes with clip AND camera) plus the event id. ──
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !streamUrl) return;
    const target = eventSeekSeconds(clip, currentEvent, camera);
    if (target <= 0) return;
    const seek = () => {
      const dur = video.duration;
      video.currentTime =
        Number.isFinite(dur) && dur > 0 ? Math.min(target, dur) : target;
    };
    if (video.readyState >= 1 /* HAVE_METADATA */) {
      seek();
    } else {
      video.addEventListener("loadedmetadata", seek, { once: true });
      return () => video.removeEventListener("loadedmetadata", seek);
    }
  }, [streamUrl, currentEvent?.id, camera, clip?.id]);

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
    if (!clip.angles.some((a) => a.camera === cam.key && isStreamableAngle(a)))
      return;
    if (cam.key === camera) return;
    setCamera(cam.key);
  };

  const cameraAvailable = (cam: CameraDef): boolean =>
    !!clip &&
    clip.angles.some((a) => a.camera === cam.key && isStreamableAngle(a));

  // ── Playlist navigation: step through the loaded events. The clip/stream/HUD
  //    effects all key off `currentEvent`, so flipping the index re-resolves the
  //    clip and reloads the video — no extra plumbing needed. ──
  const eventCount = !inDirectMode && events ? events.length : 0;
  const canPrev = index > 0;
  const canNext = index < eventCount - 1;
  const goPrev = () => setIndex((i) => Math.max(0, i - 1));
  const goNext = () => setIndex((i) => Math.min(eventCount - 1, i + 1));

  // ── Keep `index` in range as the list shrinks (e.g. after a delete). When the
  //    last clip is removed the list goes empty and `currentEvent` becomes
  //    undefined; the stream URL collapses to "" and the player shows empty. ──
  useEffect(() => {
    if (events && index > Math.max(0, events.length - 1)) {
      setIndex(Math.max(0, events.length - 1));
    }
  }, [events, index]);

  // ── Auto-dismiss the success/soft-gone notice so it doesn't linger. ──
  useEffect(() => {
    if (!notice) return;
    const id = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(id);
  }, [notice]);

  // ── Abort any in-flight delete if the screen unmounts. ──
  useEffect(() => () => deleteAbortRef.current?.abort(), []);

  // ── Dismiss an open delete confirm when the selection moves (query nav or
  //    prev/next) so a later Confirm can't act on a clip the user has navigated
  //    away from. Skipped mid-deletion so an in-flight delete isn't disturbed. ──
  useEffect(() => {
    if (deleting) return;
    setPending(null);
    setDeleteFail(null);
  }, [selectedClipId]);

  const openDeleteDialog = () => {
    if (!clipReady || !clip) return;
    const label = currentEvent
      ? `${humanize(currentEvent.type)} \u2014 ${fmtDateTime(currentEvent.t)}`
      : `${clip.folder_class} \u2014 ${fmtDateTime(clip.started_at)}`;
    setPending({ clipId: clip.id, label });
    setDeleteFail(null);
    setNotice(null);
  };

  const closeDeleteDialog = () => {
    if (deleting) return; // can't dismiss mid-flight
    setPending(null);
    setDeleteFail(null);
  };

  /** Remove the deleted clip by stable id (never by the current `index`, which
   *  can move) and clear the streamed clip if it was the one removed. In direct
   *  mode we KEEP `directClipId` so deleting an event-less clip shows the empty
   *  "deleted" state rather than snapping the playlist to events[0]. */
  const finishDeletion = (clipId: number, msg: string) => {
    setPending(null);
    setDeleteFail(null);
    setNotice(msg);
    setClip((prev) => (prev && prev.id === clipId ? null : prev));
    setEvents((prev) => (prev ? prev.filter((e) => e.clip_id !== clipId) : prev));
  };

  const confirmDelete = async () => {
    if (!pending || deleting) return;
    const clipId = pending.clipId;
    setDeleting(true);
    setDeleteFail(null);
    const ac = new AbortController();
    deleteAbortRef.current = ac;
    try {
      await api.deleteClip(clipId, ac.signal);
      finishDeletion(clipId, "Clip deleted from the car.");
    } catch (err) {
      if (ac.signal.aborted) return; // silent: the user/unmount cancelled
      const fail = classifyDeleteFailure(err);
      if (fail.softGone) finishDeletion(clipId, fail.message);
      else setDeleteFail(fail);
    } finally {
      if (deleteAbortRef.current === ac) deleteAbortRef.current = null;
      setDeleting(false);
    }
  };

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
        {clip && !clipPlayable && (
          <div class="video-unavailable-overlay" data-testid="video-unarchived">
            <Icon name="hard-drive" class="video-unavailable-icon" />
            <p class="video-unavailable-title">Not yet archived</p>
            <p class="video-unavailable-detail">
              This clip is still on the car's USB drive. Playback and download
              become available once it's archived to the device.
            </p>
          </div>
        )}
      </div>

      {/* Top overlay with location and info */}
      <div class="event-header" id="topOverlay">
        <div class="event-info">
          <h2 class="event-location">
            {currentEvent ? locationLabel(currentEvent) : clip?.folder_class ?? "\u2014"}
          </h2>
          <div class="event-datetime">
            {currentEvent
              ? fmtDateTime(currentEvent.t)
              : clip
                ? fmtDateTime(clip.started_at)
                : "\u2014"}
          </div>
          {eventCount > 1 && (
            <div class="event-nav" data-testid="event-nav">
              <button
                type="button"
                class="event-nav-btn"
                data-testid="event-nav-prev"
                onClick={goPrev}
                disabled={!canPrev}
                aria-label="Previous event"
              >
                {"\u2039"}
              </button>
              <span class="event-nav-pos" data-testid="event-nav-pos">
                {index + 1} / {eventCount}
              </span>
              <button
                type="button"
                class="event-nav-btn"
                data-testid="event-nav-next"
                onClick={goNext}
                disabled={!canNext}
                aria-label="Next event"
              >
                {"\u203A"}
              </button>
            </div>
          )}
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
          <div>{currentEvent ? humanize(currentEvent.type) : clip?.folder_class ?? "\u2014"}</div>
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

        {/* Download all angles (ZIP export) — only when archive-backed AND the
            resolved clip matches the current selection (so a query change in
            flight can't hand back a ZIP of the previously-shown clip). */}
        <a
          class={`camera-option download-option${clipPlayable && clipReady ? "" : " disabled"}`}
          id="downloadButton"
          href={clipPlayable && clipReady && clip ? api.exportUrl(clip.id) : undefined}
          download
          aria-disabled={clipPlayable && clipReady ? "false" : "true"}
        >
          <Icon name="download" class="camera-icon" />
          <div class="camera-label">Download All</div>
        </a>

        <a
          class={`camera-option download-option${angleDownloadReady ? "" : " disabled"}`}
          id="downloadAngleButton"
          href={angleDownloadReady && clip ? api.downloadUrl(clip.id, camera) : undefined}
          download={angleDownloadReady ? true : undefined}
          aria-disabled={angleDownloadReady ? "false" : "true"}
        >
          <Icon name="download" class="camera-icon" />
          <div class="camera-label">Download Angle</div>
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

        {/* Delete clip — operator-gated destructive action (webd car-handoff). */}
        <button
          type="button"
          class={`camera-option delete-option${clipReady ? "" : " disabled"}`}
          id="deleteButton"
          onClick={openDeleteDialog}
          disabled={!clipReady || deleting}
          aria-disabled={clipReady ? "false" : "true"}
          aria-haspopup="dialog"
          title={clipReady ? "Delete this clip from the car" : "No clip to delete"}
        >
          <Icon name="trash-2" class="camera-icon" />
          <div class="camera-label">Delete</div>
        </button>
      </div>

      {/* Operator-gated delete confirmation (names the clip; no one-click delete). */}
      {pending && (
        <div
          class="delete-modal-backdrop"
          role="presentation"
          onClick={closeDeleteDialog}
        >
          <div
            class="delete-modal"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="deleteModalTitle"
            aria-describedby="deleteModalDesc"
            data-testid="delete-dialog"
            onClick={(e: Event) => e.stopPropagation()}
          >
            <h3 id="deleteModalTitle" class="delete-modal-title">
              Delete this clip?
            </h3>
            <p id="deleteModalDesc" class="delete-modal-desc">
              This permanently removes{" "}
              <strong class="delete-modal-clip">{pending.label}</strong> from the
              car's USB drive. This can't be undone.
            </p>

            {deleteFail && (
              <div
                class={`delete-modal-status${deleteFail.retryable ? " retryable" : " fatal"}`}
                role="alert"
                data-testid="delete-error"
              >
                {deleteFail.message}
              </div>
            )}

            <div class="delete-modal-actions">
              <button
                type="button"
                class="delete-modal-btn cancel"
                onClick={closeDeleteDialog}
                disabled={deleting}
              >
                {deleteFail && !deleteFail.retryable ? "Close" : "Cancel"}
              </button>
              {(!deleteFail || deleteFail.retryable) && (
                <button
                  type="button"
                  class="delete-modal-btn confirm"
                  data-testid="delete-confirm"
                  onClick={confirmDelete}
                  disabled={deleting}
                  aria-busy={deleting ? "true" : "false"}
                >
                  {deleting ? (
                    <>
                      <span class="delete-spinner" aria-hidden="true" /> Deleting
                      {"\u2026"}
                    </>
                  ) : deleteFail ? (
                    "Retry"
                  ) : (
                    "Delete"
                  )}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {notice && (
        <div class="event-player-notice" role="status" data-testid="delete-notice">
          {notice}
        </div>
      )}

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
