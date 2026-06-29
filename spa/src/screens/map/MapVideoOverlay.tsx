import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api } from "../../api/client";
import type { Clip } from "../../api/types";
import { Icon } from "../../components/Icon";
import { isDownloadableAngle, isStreamableAngle } from "../../player/angles";
import { classifyDeleteFailure } from "../../player/deleteClip";

interface MapVideoOverlayProps {
  clip: Clip;
  clips: readonly Clip[];
  camera: string;
  cloudConnected: boolean;
  clock: string;
  onClose: () => void;
  onNavigate: (direction: -1 | 1) => void;
  onCameraChange: (camera: string) => void;
  onDeleteClip: (clipId: number) => Promise<void>;
}

interface OverlayCamera {
  key: string;
  label: string;
  icon: string;
}

const OVERLAY_CAMERAS: OverlayCamera[] = [
  { key: "front", label: "Front", icon: "chevron-up" },
  { key: "back", label: "Back", icon: "chevron-down" },
  { key: "left_repeater", label: "Left", icon: "chevron-left" },
  { key: "right_repeater", label: "Right", icon: "chevron-right" },
  { key: "left_pillar", label: "L Pillar", icon: "arrow-down-left" },
  { key: "right_pillar", label: "R Pillar", icon: "arrow-down-right" },
];

const STREAM_GONE_NOTICE =
  "This clip stream is no longer available. It may have changed or rolled off. Reload and try again.";
const NO_PLAYABLE_NOTICE = "No playable camera stream for this clip.";
const OVERLAY_MARGIN = 10;

function clipFilename(clip: Clip, camera: string): string {
  const raw = clip.canonical_key.split("/").pop() ?? `clip-${clip.id}`;
  const noExt = raw.replace(/\.mp4$/i, "");
  const base = noExt.replace(
    /-(front|back|left_repeater|right_repeater|left_pillar|right_pillar)$/i,
    "",
  );
  return `${base || noExt}-${camera}.mp4`;
}

function clampPosition(
  left: number,
  top: number,
  width: number,
  height: number,
): { left: number; top: number } {
  const maxLeft = Math.max(OVERLAY_MARGIN, window.innerWidth - width - OVERLAY_MARGIN);
  const maxTop = Math.max(OVERLAY_MARGIN, window.innerHeight - height - OVERLAY_MARGIN);
  return {
    left: Math.min(Math.max(OVERLAY_MARGIN, left), maxLeft),
    top: Math.min(Math.max(OVERLAY_MARGIN, top), maxTop),
  };
}

export function MapVideoOverlay({
  clip,
  clips,
  camera,
  cloudConnected,
  clock: _clock,
  onClose,
  onNavigate,
  onCameraChange,
  onDeleteClip,
}: MapVideoOverlayProps) {
  const overlayRef = useRef<HTMLDivElement>(null);
  const headerRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const probeSeqRef = useRef(0);
  const [maximized, setMaximized] = useState(false);
  const [position, setPosition] = useState(() => ({
    left: Math.round(window.innerWidth / 3),
    top: Math.round(window.innerHeight / 4),
  }));
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [streamNotice, setStreamNotice] = useState<string | null>(null);
  const [probedStreamUrl, setProbedStreamUrl] = useState("");
  const dragCleanupRef = useRef<(() => void) | null>(null);
  const positionRef = useRef(position);

  const currentAngle = clip.angles.find((angle) => angle.camera === camera);
  const streamCandidateUrl =
    clip && isStreamableAngle(currentAngle) ? api.streamUrl(clip.id, camera) : "";
  const shouldProbeStream = !!currentAngle && !isDownloadableAngle(currentAngle);
  const streamUrl =
    clip && streamCandidateUrl
      ? shouldProbeStream
        ? probedStreamUrl === streamCandidateUrl
          ? probedStreamUrl
          : ""
        : streamCandidateUrl
      : "";
  const canPlayAny = clip.angles.some(isStreamableAngle);
  const clipIndex = clips.findIndex((item) => item.id === clip.id);
  const canPrev = clipIndex > 0;
  const canNext = clipIndex >= 0 && clipIndex < clips.length - 1;

  useEffect(() => {
    positionRef.current = position;
  }, [position]);

  const title = useMemo(() => clipFilename(clip, camera), [clip, camera]);
  const coords =
    clip.lat != null && clip.lon != null
      ? `Location ${clip.lat.toFixed(4)}, ${clip.lon.toFixed(4)}`
      : null;

  useEffect(() => {
    if (maximized) return;
    const overlay = overlayRef.current;
    if (!overlay) return;
    const rect = overlay.getBoundingClientRect();
    setPosition((prev) => clampPosition(prev.left, prev.top, rect.width, rect.height));
  }, [maximized, clip.id, camera]);

  useEffect(() => {
    if (!canPlayAny) {
      setProbedStreamUrl("");
      setStreamNotice(null);
      return;
    }
    if (!streamCandidateUrl) {
      setProbedStreamUrl("");
      setStreamNotice("Video is unavailable for this clip.");
      return;
    }
    if (!shouldProbeStream) {
      setProbedStreamUrl("");
      setStreamNotice(null);
      return;
    }
    const seq = ++probeSeqRef.current;
    const ac = new AbortController();
    (async () => {
      try {
        const resp = await fetch(streamCandidateUrl, {
          method: "HEAD",
          credentials: "same-origin",
          signal: ac.signal,
        });
        if (ac.signal.aborted || seq !== probeSeqRef.current) return;
        if (!resp.ok) {
          setProbedStreamUrl("");
          setStreamNotice(STREAM_GONE_NOTICE);
          return;
        }
        setProbedStreamUrl(streamCandidateUrl);
        setStreamNotice(null);
      } catch {
        if (ac.signal.aborted || seq !== probeSeqRef.current) return;
        setProbedStreamUrl("");
        setStreamNotice(STREAM_GONE_NOTICE);
      }
    })();
    return () => ac.abort();
  }, [canPlayAny, clip.id, shouldProbeStream, streamCandidateUrl]);

  useEffect(
    () => () => {
      probeSeqRef.current += 1;
      dragCleanupRef.current?.();
      const video = videoRef.current;
      if (!video) return;
      video.pause();
      video.removeAttribute("src");
      video.load();
    },
    [],
  );

  useEffect(() => {
    const onResize = () => {
      if (maximized) return;
      const overlay = overlayRef.current;
      if (!overlay) return;
      const rect = overlay.getBoundingClientRect();
      setPosition((prev) => clampPosition(prev.left, prev.top, rect.width, rect.height));
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [maximized]);

  useEffect(() => {
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const overlay = overlayRef.current;
      if (!overlay?.classList.contains("maximized")) return;
      setMaximized(false);
    };
    window.addEventListener("keydown", onKeydown);
    return () => window.removeEventListener("keydown", onKeydown);
  }, []);

  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      const overlay = overlayRef.current;
      if (!overlay) return;
      const target = event.target as Node | null;
      if (target && overlay.contains(target)) return;
      onClose();
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [onClose]);

  useEffect(() => {
    if (maximized) return;
    const header = headerRef.current;
    const overlay = overlayRef.current;
    if (!header || !overlay) return;

    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      const rect = overlay.getBoundingClientRect();
      const startLeft = positionRef.current.left;
      const startTop = positionRef.current.top;
      const startX = event.clientX;
      const startY = event.clientY;

      const onPointerMove = (moveEvent: PointerEvent) => {
        const next = clampPosition(
          startLeft + (moveEvent.clientX - startX),
          startTop + (moveEvent.clientY - startY),
          rect.width,
          rect.height,
        );
        setPosition(next);
        positionRef.current = next;
      };
      const onPointerUp = () => {
        dragCleanupRef.current?.();
      };
      const cleanup = () => {
        document.removeEventListener("pointermove", onPointerMove);
        document.removeEventListener("pointerup", onPointerUp);
        if (dragCleanupRef.current === cleanup) dragCleanupRef.current = null;
      };
      dragCleanupRef.current?.();
      dragCleanupRef.current = cleanup;

      document.addEventListener("pointermove", onPointerMove);
      document.addEventListener("pointerup", onPointerUp);
    };

    header.addEventListener("pointerdown", onPointerDown);
    return () => {
      header.removeEventListener("pointerdown", onPointerDown);
      dragCleanupRef.current?.();
    };
  }, [maximized]);

  const onFullscreen = () => {
    const stage = stageRef.current;
    const video = videoRef.current;
    if (stage && stage.requestFullscreen) {
      void stage.requestFullscreen();
      return;
    }
    if (video?.requestFullscreen) {
      void video.requestFullscreen();
    }
  };

  const onDelete = async () => {
    if (!window.confirm(`Delete "${clip.canonical_key}" and all its camera angles?`)) return;
    setActionNotice(null);
    setDeleting(true);
    try {
      await onDeleteClip(clip.id);
    } catch (err) {
      const fail = classifyDeleteFailure(err);
      const suffix = fail.retryable ? " Retry in a moment." : "";
      setActionNotice(`Couldn't delete clip. ${fail.message}${suffix}`);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div
      class={`video-overlay${maximized ? " maximized" : ""}`}
      id="videoOverlay"
      data-testid="video-overlay"
      ref={overlayRef}
      style={maximized ? undefined : `left: ${position.left}px; top: ${position.top}px;`}
    >
      <div
        class="video-overlay-header"
        ref={headerRef}
        style={`cursor: ${maximized ? "default" : "grab"};`}
      >
        <span id="overlayTitle">{title}</span>
        <button
          class="close-btn"
          aria-label="Close video overlay"
          data-testid="vp-overlay-close"
          onClick={onClose}
        >
          <Icon name="x" />
        </button>
      </div>
      <div class="cam-switcher" id="camSwitcher">
        {OVERLAY_CAMERAS.map((cam) => {
          const available = clip.angles.some(
            (angle) => angle.camera === cam.key && isStreamableAngle(angle),
          );
          const active = cam.key === camera && available;
          return (
            <button
              class={`cam-btn${active ? " active" : ""}`}
              data-angle={cam.key}
              title={cam.label}
              aria-label={cam.label}
              data-testid={`vp-overlay-cam-${cam.key}`}
              disabled={!available}
              onClick={() => available && onCameraChange(cam.key)}
            >
              <Icon name={cam.icon} class="cam-icon" />
              <span class="cam-label">{cam.label}</span>
            </button>
          );
        })}
      </div>
      <div class="video-overlay-stage" ref={stageRef}>
        <video
          id="overlayVideo"
          ref={videoRef}
          controls
          autoplay
          muted
          controlsList="nofullscreen"
          disablePictureInPicture
          src={streamUrl || undefined}
          data-testid="vp-overlay-video"
        />
        {!canPlayAny && (
          <div class="video-unavailable-overlay" data-testid="video-unarchived">
            <Icon name="hard-drive" class="video-unavailable-icon" />
            <p class="video-unavailable-title">Video unavailable</p>
            <p class="video-unavailable-detail">{NO_PLAYABLE_NOTICE}</p>
          </div>
        )}
        <div class="overlay-hud" id="overlayHud">
          <div class="oh-gear" id="olGear">P</div>
          <div class="oh-pedal oh-brake" id="olBrake" style="--oh-fill: 0%;">
            <span class="oh-fill"><i /></span>
            <span class="oh-lbl">B</span>
          </div>
          <span class="oh-blinker left" id="olBlinkerL">◀</span>
          <div class="oh-speed">
            <span class="oh-speed-val" id="olSpeedVal">0</span>
            <span class="oh-speed-unit" id="olSpeedUnit">mph</span>
          </div>
          <span class="oh-blinker right" id="olBlinkerR">▶</span>
          <div class="oh-wheel" id="olWheel" style="--oh-wheel: 0deg;">
            <svg viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="8" stroke="#fff" strokeWidth="1.4" />
              <path d="M6.8 9.8H17.2" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
              <path d="M12 9.8V16.8" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
              <circle cx="12" cy="12" r="1.8" stroke="#fff" strokeWidth="1.4" />
            </svg>
          </div>
          <div class="oh-pedal oh-throttle" id="olThrottle" style="--oh-fill: 0%;">
            <span class="oh-fill"><i /></span>
            <span class="oh-lbl">A</span>
          </div>
          <div class="oh-ap" id="olAP2" />
        </div>
        {canPlayAny && streamNotice && (
          <div class="video-unavailable-overlay" data-testid="video-stream-unavailable">
            <Icon name="hard-drive" class="video-unavailable-icon" />
            <p class="video-unavailable-title">Video unavailable</p>
            <p class="video-unavailable-detail">{streamNotice}</p>
          </div>
        )}
        {actionNotice && (
          <div class="video-unavailable-overlay">
            <Icon name="alert-triangle" class="video-unavailable-icon" />
            <p class="video-unavailable-title">Action failed</p>
            <p class="video-unavailable-detail">{actionNotice}</p>
          </div>
        )}
      </div>
      <div class="video-overlay-info" id="overlayInfo">
        {coords && <span id="olCoords" data-testid="vp-overlay-coords">{coords}</span>}
        <div class="overlay-nav">
          <button
            class="nav-btn"
            title="Previous clip"
            aria-label="Previous clip"
            data-testid="vp-overlay-prev"
            disabled={!canPrev}
            onClick={() => onNavigate(-1)}
          >
            <Icon name="chevron-left" />
          </button>
          <button
            class="nav-btn"
            title="Next clip"
            aria-label="Next clip"
            data-testid="vp-overlay-next"
            disabled={!canNext}
            onClick={() => onNavigate(1)}
          >
            <Icon name="chevron-right" />
          </button>
          <a
            class="nav-btn"
            href={api.exportUrl(clip.id)}
            title="Download ZIP"
            aria-label="Download ZIP"
            data-testid="vp-overlay-download"
          >
            <Icon name="download" />
          </a>
          {cloudConnected && (
            <button
              class="nav-btn nav-btn-cloud"
              id="archiveNavBtn"
              title="Archive to Cloud"
              aria-label="Archive to cloud"
              data-testid="vp-overlay-archive"
              onClick={() => {}}
            >
              <Icon name="cloud" />
            </button>
          )}
          <button
            class="nav-btn"
            title="Fullscreen video"
            aria-label="Fullscreen video"
            data-testid="vp-overlay-fullscreen"
            onClick={onFullscreen}
          >
            <Icon name="maximize" />
          </button>
          <button
            class="nav-btn"
            id="overlayMaximizeBtn"
            title="Maximize"
            aria-label="Maximize video overlay"
            data-testid="vp-overlay-maximize"
            onClick={() => setMaximized((prev) => !prev)}
          >
            <Icon name="maximize-2" />
          </button>
          <button
            class="nav-btn nav-btn-danger"
            title="Delete"
            aria-label="Delete event"
            data-testid="vp-overlay-delete"
            disabled={deleting}
            onClick={() => void onDelete()}
          >
            <Icon name="trash-2" />
          </button>
        </div>
      </div>
    </div>
  );
}
