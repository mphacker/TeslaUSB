import { useEffect, useRef, useState } from "preact/hooks";
import { Fragment } from "preact";
import { Icon } from "../components/Icon";
import { api, ApiError } from "../api/client";
import type {
  Analytics as AnalyticsData,
  EncryptionStatus,
  FilesystemEntry,
  GovernorInfo,
  StorageHealth as StorageHealthDto,
  StorageInfo,
  SystemHealth,
  SystemMetrics,
  UsbVolumeInfo,
} from "../api/types";
import { AnalyticsCharts, type AnalyticsChartModel } from "../charts/controller";
import "../styles/analytics.css";
import "../styles/storage.css";

const METERS_PER_MILE = 1609.344;
const MPH_PER_MPS = 2.2369362920544;
const EM_DASH = "\u2014";
const DASH = "\u2014";

/** Mounts we treat as the device's primary data volume, most-preferred first. */
const PRIMARY_MOUNT_HINTS = ["/mnt/teslausb", "/mnt/cam", "/data", "/mnt", "/"];
/** Pseudo/virtual filesystem types we never pick as the "primary" volume. */
const VIRTUAL_FSTYPES = new Set([
  "tmpfs",
  "devtmpfs",
  "overlay",
  "squashfs",
  "proc",
  "sysfs",
  "ramfs",
  "devpts",
  "cgroup",
  "cgroup2",
]);

/** Bytes → a compact human string (1000-based for analytics, 1024-based for storage). */
function humanBytes(n: number): string {
  if (n < 1000) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1000;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

/** Bytes → a compact human string (1024-based for storage). */
function humanBytesBinary(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return DASH;
  const gib = n / 1024 ** 3;
  if (gib >= 1) return `${gib.toFixed(gib >= 10 ? 0 : 1)} GB`;
  return `${(n / 1024 ** 2).toFixed(0)} MB`;
}

/** Clamp a percentage into [0, 100]; NaN/inf → 0. */
function clampPct(p: number): number {
  if (!Number.isFinite(p)) return 0;
  return Math.min(100, Math.max(0, p));
}

/** Seconds → "up Xd Yh Zm" (drops leading zero day/hour units). */
function formatUptime(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s) || s < 0) return DASH;
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const parts: string[] = [];
  if (d) parts.push(`${d}d`);
  if (h || d) parts.push(`${h}h`);
  parts.push(`${m}m`);
  return `up ${parts.join(" ")}`;
}

/** Epoch-seconds → a short local clock string for the "Updated …" footer. */
function formatUpdated(epoch: number | null | undefined): string {
  if (epoch == null || !Number.isFinite(epoch)) return DASH;
  return new Date(epoch * 1000).toLocaleTimeString();
}

/** SoC temperature → "47.2 °C" (or em-dash when no sensor). */
function formatTemp(c: number | null | undefined): string {
  if (c == null || !Number.isFinite(c)) return DASH;
  return `${c.toFixed(1)} \u00b0C`;
}

/** Coarse thermal band for the detail line. The Pi soft-throttles around 80 °C
 *  and hard-throttles at 85 °C, so warn well below that. */
function tempBand(c: number | null | undefined): string {
  if (c == null || !Number.isFinite(c)) return "\u00a0";
  if (c >= 80) return "Throttling";
  if (c >= 70) return "Warm";
  return "Nominal";
}

/** Governor stop reason → friendly text. */
const GOV_STOP_LABEL: Record<string, string> = {
  already_healthy: "already healthy",
  target_reached: "reached target",
  byte_cap: "hit per-cycle byte cap",
  count_cap: "hit per-cycle count cap",
  wall_cap: "hit per-cycle time cap",
  shutdown: "interrupted by shutdown",
  no_safe_candidate: "no evictable footage",
  anomaly_refused: "refused (disk anomaly)",
  delete_failed: "delete failed",
  stat_check_failed: "disk re-check failed",
};

/** recency_floor_secs → "1 h" / "45 min" protection window. */
function formatProtectWindow(secs: number | null | undefined): string {
  if (secs == null || !Number.isFinite(secs) || secs <= 0) return DASH;
  if (secs >= 3600) {
    const h = secs / 3600;
    return `${h % 1 === 0 ? h.toFixed(0) : h.toFixed(1)} h`;
  }
  return `${Math.round(secs / 60)} min`;
}

/** Severity → human label + the CSS modifier suffix used by the badge/dot. */
const SEV_LABEL: Record<string, string> = {
  ok: "Healthy",
  warn: "Degraded",
  error: "Attention needed",
  unknown: "Unknown",
};
const SEV_COLORS: Record<string, string> = {
  ok: "var(--accent-success, #2ea043)",
  warn: "var(--accent-warning, #d29922)",
  error: "var(--accent-error,   #f85149)",
  unknown: "var(--text-secondary, #888)",
};

function sevKey(sev: string | null | undefined): "ok" | "warn" | "error" | "unknown" {
  return sev === "ok" || sev === "warn" || sev === "error" ? sev : "unknown";
}

/** Used fraction (0..1) of a filesystem, or null when it can't be computed. */
function usedFraction(total: number, free: number): number | null {
  if (!Number.isFinite(total) || total <= 0) return null;
  if (!Number.isFinite(free) || free < 0) return null;
  const used = Math.max(0, total - free);
  return Math.min(1, used / total);
}

/** Pick the device's primary data filesystem deterministically: a known data
 *  mount first, else the largest real (non-virtual) filesystem, else the first
 *  filesystem present. Returns null when the list is empty. */
function primaryFs(filesystems: FilesystemEntry[]): FilesystemEntry | null {
  if (filesystems.length === 0) return null;
  for (const hint of PRIMARY_MOUNT_HINTS) {
    const hit = filesystems.find((f) => f.mount === hint);
    if (hit) return hit;
  }
  const real = filesystems.filter((f) => !VIRTUAL_FSTYPES.has(f.fstype));
  const pool = real.length > 0 ? real : filesystems;
  return pool.reduce((a, b) => (b.total_bytes > a.total_bytes ? b : a));
}

/** Map a used fraction to the bar's colour class (calm < 75% < warn < 90% crit). */
function capClass(frac: number): string {
  if (frac >= 0.9) return "cap-seg-used-crit";
  if (frac >= 0.75) return "cap-seg-used-warn";
  return "cap-seg-used";
}

/** A used/total memory or swap tile, degrading honestly when unreadable. */
function memTile(
  m: { total_bytes: number; available_bytes: number; used_pct: number } | null,
  emptyDetail: string,
): { value: string; detail: string } {
  if (!m || m.total_bytes <= 0) return { value: DASH, detail: emptyDetail };
  const used = Math.max(0, m.total_bytes - m.available_bytes);
  return {
    value: `${Math.round(clampPct(m.used_pct))}%`,
    detail: `${humanBytesBinary(used)} / ${humanBytesBinary(m.total_bytes)}`,
  };
}

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function mph(mps: number): string {
  return (mps * MPH_PER_MPS).toFixed(1);
}

/** "RecentClips" → "Recent Clips" for display. */
function folderLabel(folderClass: string): string {
  return folderClass.replace(/([a-z])([A-Z])/g, "$1 $2");
}

function humanize(type: string): string {
  return type
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function toChartModel(a: AnalyticsData): AnalyticsChartModel {
  return {
    events: {
      labels: a.events_by_type.map((e) => humanize(e.type)),
      values: a.events_by_type.map((e) => e.count),
    },
    trips: {
      labels: a.trips_by_day.map((d) => d.day),
      values: a.trips_by_day.map((d) => d.count),
    },
  };
}

function errMessage(err: unknown): string {
  return err instanceof ApiError
    ? `${err.code}: ${err.message}`
    : (err as Error).message;
}

function buildId(): string {
  return (
    (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__ ??
    "dev"
  );
}

/** Find a USB volume by role ("dashcam" | "media"); null when not reported. */
function pickVolume(volumes: UsbVolumeInfo[], role: string): UsbVolumeInfo | null {
  return volumes.find((v) => v.role === role) ?? null;
}

type BannerLevel = "ok" | "warn" | "crit" | "unknown";

/** The one loss-critical fact for the user: is there room left to keep archiving
 *  drives? Driven purely by the SD card's free fraction (the archive lives there).
 *  crit < 5% free, warn < 12% free, else ok; unknown when no filesystem reported. */
function recordingBanner(
  primary: FilesystemEntry | null,
  governor: GovernorInfo | null,
): { level: BannerLevel; title: string; detail: string } {
  const frac = primary != null ? usedFraction(primary.total_bytes, primary.free_bytes) : null;
  if (primary == null || frac == null) {
    return {
      level: "unknown",
      title: "Storage status unknown",
      detail: "No readable filesystem was reported, so recording headroom can't be verified.",
    };
  }
  const freeFrac = 1 - frac;
  const freeText = humanBytesBinary(primary.free_bytes);
  if (governor != null && governor.mode === "armed") {
    if (freeFrac >= governor.target_free_frac) {
      return {
        level: "ok",
        title: "Recording protected",
        detail: `${freeText} free — auto-cleanup keeps the newest footage and trims the oldest to hold this level.`,
      };
    }
    if (freeFrac >= 0.05) {
      return {
        level: "warn",
        title: "Auto-cleanup catching up",
        detail: `${freeText} free, below the ${(governor.target_free_frac * 100).toFixed(0)}% target. If this persists, recent footage may be protected from deletion.`,
      };
    }
    return {
      level: "crit",
      title: "Archive almost full",
      detail: `Only ${freeText} free — auto-cleanup can't free enough space. New drives may not be saved.`,
    };
  }
  if (freeFrac < 0.05) {
    return {
      level: "crit",
      title: "Archive almost full",
      detail: `Only ${freeText} free. New drives may not be saved — free up space or turn on auto-cleanup now.`,
    };
  }
  if (freeFrac < 0.12) {
    return {
      level: "warn",
      title: "Archive space getting low",
      detail: `${freeText} free. Free up space or turn on auto-cleanup before the card fills and archiving stops.`,
    };
  }
  return {
    level: "ok",
    title: "Recording protected",
    detail: `${freeText} free on the SD card — there's room to keep archiving your drives.`,
  };
}

const BANNER_ICON: Record<BannerLevel, string> = {
  ok: "shield",
  warn: "zap",
  crit: "zap",
  unknown: "hard-drive",
};

/** Top-of-page recording-safety banner. */
function RecordingBanner({
  primary,
  governor,
}: {
  primary: FilesystemEntry | null;
  governor: GovernorInfo | null;
}) {
  const b = recordingBanner(primary, governor);
  return (
    <section
      class={`storage-banner storage-banner-${b.level}`}
      id="storage-recording-banner"
      data-banner-level={b.level}
      role="status"
    >
      <span class="storage-banner-icon" aria-hidden="true">
        <Icon name={BANNER_ICON[b.level]} />
      </span>
      <div class="storage-banner-text">
        <span class="storage-banner-title">{b.title}</span>
        <span class="storage-banner-detail">{b.detail}</span>
      </div>
    </section>
  );
}

function EncryptionBanner({ enc }: { enc: EncryptionStatus | null }) {
  if (!enc || !enc.encrypting) return null;
  return (
    <section
      class="storage-banner storage-banner-warn"
      id="storage-encryption-banner"
      data-banner-level="warn"
      role="status"
    >
      <span class="storage-banner-icon" aria-hidden="true">
        <Icon name={BANNER_ICON.warn} />
      </span>
      <div class="storage-banner-text">
        <span class="storage-banner-title">Dashcam encryption is on</span>
        <span class="storage-banner-detail">
          Your Tesla is encrypting its dashcam clips, so TeslaUSB can't save or
          play them. To let TeslaUSB archive your footage again, turn off
          Controls → Safety → Encrypt Dashcam Recordings in the car. Clips
          recorded while encryption was on stay viewable only in the car or at
          Tesla's dashcam viewer.
        </span>
      </div>
    </section>
  );
}

function SeverityBadge({ severity }: { severity: string | null | undefined }) {
  const key = sevKey(severity);
  return (
    <span class={`storage-badge storage-badge-${key}`} data-severity={key}>
      <span class="storage-dot" style={`background:${SEV_COLORS[key]};`} aria-hidden="true" />
      {SEV_LABEL[key]}
    </span>
  );
}

/** A single filesystem's used/free capacity bar + figures. */
function FilesystemRow({ fs }: { fs: FilesystemEntry }) {
  const frac = usedFraction(fs.total_bytes, fs.free_bytes);
  const usedBytes = frac == null ? null : Math.max(0, fs.total_bytes - fs.free_bytes);
  const pctText = frac == null ? DASH : `${Math.round(frac * 100)}%`;
  return (
    <div class="fs-item" data-fs-mount={fs.mount}>
      <div class="fs-item-head">
        <span class="fs-mount">{fs.mount || DASH}</span>
        <span class="fs-meta">
          {humanBytesBinary(usedBytes)} / {humanBytesBinary(fs.total_bytes)} used ({pctText})
        </span>
      </div>
      <div
        class="cap-bar"
        role="img"
        aria-label={`${fs.mount}: ${pctText} used (${humanBytesBinary(usedBytes)} of ${humanBytesBinary(fs.total_bytes)})`}
      >
        {frac != null && (
          <div
            class={`cap-seg ${capClass(frac)}`}
            style={`width:${(frac * 100).toFixed(2)}%`}
            data-fs-used
          />
        )}
        <div class="cap-seg cap-seg-free" style="flex:1" />
      </div>
      <div class="fs-meta">
        {fs.device || DASH} · {fs.fstype || DASH} · {humanBytesBinary(fs.free_bytes)} free
      </div>
    </div>
  );
}

/** The two USB image files (dashcam + media) and the archived-footage library all
 *  live on the one SD card. This card makes that composition explicit so a
 *  near-full card is understandable and actionable, not mysterious. Falls back to
 *  a plain used/free bar when the pre-allocated volume sizes aren't reported. */
function SdCompositionCard({
  primary,
  dashcam,
  media,
}: {
  primary: FilesystemEntry | null;
  dashcam: UsbVolumeInfo | null;
  media: UsbVolumeInfo | null;
}) {
  const frac = primary != null ? usedFraction(primary.total_bytes, primary.free_bytes) : null;

  if (primary == null || frac == null) {
    return (
      <section class="storage-card" id="storage-sdcard-card">
        <div class="storage-card-head">
          <h2 class="storage-card-title">
            <Icon name="database" /> Saved footage — SD card
          </h2>
        </div>
        <p class="storage-note" data-testid="storage-sdcard-degraded">
          Capacity is unavailable — no readable filesystem was reported.
        </p>
      </section>
    );
  }

  const total = primary.total_bytes;
  const free = primary.free_bytes;
  const usedBytes = Math.max(0, total - free);
  const dashTotal = dashcam?.total_bytes ?? null;
  const mediaTotal = media?.total_bytes ?? null;
  const canCompose =
    dashTotal != null && mediaTotal != null && dashTotal + mediaTotal + free <= total;
  const systemBytes = canCompose ? Math.max(0, total - dashTotal! - mediaTotal! - free) : null;
  const pct = (b: number) => `${((b / total) * 100).toFixed(2)}%`;

  return (
    <section class="storage-card" id="storage-sdcard-card">
      <div class="storage-card-head">
        <h2 class="storage-card-title">
          <Icon name="database" /> Saved footage — SD card
        </h2>
        <span class="storage-pill storage-pill-muted">{primary.mount || DASH}</span>
      </div>
      <div class="cap-figure">
        <span class="cap-figure-value" data-sd-free>
          {humanBytesBinary(free)}
        </span>
        <span class="cap-figure-label">free of {humanBytesBinary(total)}</span>
      </div>

      {canCompose && systemBytes != null ? (
        <>
          <div
            class="comp-bar"
            id="storage-composition"
            role="img"
            aria-label={`SD card composition: dashcam buffer ${humanBytesBinary(dashTotal)}, media library ${humanBytesBinary(mediaTotal)}, system and archived footage ${humanBytesBinary(systemBytes)}, free ${humanBytesBinary(free)} of ${humanBytesBinary(total)}`}
          >
            <div class="comp-seg comp-dashcam" style={`width:${pct(dashTotal!)}`} data-comp="dashcam" />
            <div class="comp-seg comp-media" style={`width:${pct(mediaTotal!)}`} data-comp="media" />
            <div class="comp-seg comp-system" style={`width:${pct(systemBytes)}`} data-comp="system" />
            <div class="comp-seg comp-free" style="flex:1" data-comp="free" />
          </div>
          <div class="comp-legend">
            <span class="comp-legend-item">
              <i class="comp-swatch comp-dashcam" /> Dashcam buffer {humanBytesBinary(dashTotal)}
            </span>
            <span class="comp-legend-item">
              <i class="comp-swatch comp-media" /> Media library {humanBytesBinary(mediaTotal)}
            </span>
            <span class="comp-legend-item">
              <i class="comp-swatch comp-system" /> System &amp; archived footage {humanBytesBinary(systemBytes)}
            </span>
            <span class="comp-legend-item">
              <i class="comp-swatch comp-free" /> Free {humanBytesBinary(free)}
            </span>
          </div>
          <p class="storage-help">
            The dashcam buffer is space Tesla reserved up front, so the card reads
            near-full even when there's still room to archive. "System &amp; archived
            footage" is your saved drive library plus the operating system.
          </p>
        </>
      ) : (
        <>
          <div
            class="cap-bar"
            role="img"
            aria-label={`SD card: ${Math.round(frac * 100)}% used (${humanBytesBinary(usedBytes)} of ${humanBytesBinary(total)})`}
          >
            <div
              class={`cap-seg ${capClass(frac)}`}
              style={`width:${(frac * 100).toFixed(2)}%`}
              data-sd-used
            />
            <div class="cap-seg cap-seg-free" style="flex:1" />
          </div>
          <div class="cap-legend">
            <span>
              <i class={`cap-swatch ${capClass(frac)}`} /> Used {humanBytesBinary(usedBytes)}
            </span>
            <span>
              <i class="cap-swatch cap-seg-free" style="border:1px solid var(--border-input)" /> Free{" "}
              {humanBytesBinary(free)}
            </span>
          </div>
        </>
      )}
    </section>
  );
}

/** A single USB drive (TESLACAM / MEDIA) capacity card. Degrades to a note when
 *  its byte figures are null (measurement source down). */
function VolumeCard({
  id,
  icon,
  title,
  subtitle,
  volume,
}: {
  id: string;
  icon: string;
  title: string;
  subtitle: string;
  volume: UsbVolumeInfo | null;
}) {
  const total = volume?.total_bytes ?? null;
  const free = volume?.free_bytes ?? null;
  const used = volume?.used_bytes ?? null;
  const frac = total != null && free != null ? usedFraction(total, free) : null;

  return (
    <section class="storage-card cap-card" id={id}>
      <div class="storage-card-head">
        <h2 class="storage-card-title">
          <Icon name={icon} /> {title}
        </h2>
      </div>
      <p class="storage-help">{subtitle}</p>
      {total == null || free == null || frac == null ? (
        <p class="storage-note" data-testid={`${id}-degraded`}>
          Capacity is unavailable — the drive's measurement source isn't reporting
          right now.
        </p>
      ) : (
        <>
          <div class="cap-figure">
            <span class="cap-figure-value">{humanBytesBinary(free)}</span>
            <span class="cap-figure-label">free of {humanBytesBinary(total)}</span>
          </div>
          <div
            class="cap-bar"
            role="img"
            aria-label={`${title}: ${Math.round(frac * 100)}% used (${humanBytesBinary(used)} of ${humanBytesBinary(total)})`}
          >
            <div
              class={`cap-seg ${capClass(frac)}`}
              style={`width:${(frac * 100).toFixed(2)}%`}
              data-vol-used
            />
            <div class="cap-seg cap-seg-free" style="flex:1" />
          </div>
          <div class="fs-meta" data-vol-meta>
            {humanBytesBinary(used)} used ({Math.round(frac * 100)}%)
            {volume && !volume.stable ? " · measured while recording — approximate" : ""}
          </div>
        </>
      )}
    </section>
  );
}

export function Analytics() {
  const eventsCanvasRef = useRef<HTMLCanvasElement>(null);
  const tripsCanvasRef = useRef<HTMLCanvasElement>(null);
  const chartsRef = useRef<AnalyticsCharts | null>(null);

  // Storage state
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [health, setHealth] = useState<StorageHealthDto | null>(null);
  const [enc, setEnc] = useState<EncryptionStatus | null>(null);
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);
  const [sysHealth, setSysHealth] = useState<SystemHealth | null>(null);

  // Analytics state
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [analyticsError, setAnalyticsError] = useState<string | null>(null);

  // ── Mount: fetch both storage and analytics independently (each self-degrades). ──
  useEffect(() => {
    const ctrl = new AbortController();
    // Storage probes
    Promise.all([
      api.storage(ctrl.signal).then(setInfo).catch(() => {}),
      api.storageHealth(ctrl.signal).then(setHealth).catch(() => {}),
      api.encryptionStatus(ctrl.signal).then(setEnc).catch(() => {}),
      api.systemMetrics(ctrl.signal).then(setMetrics).catch(() => {}),
      api.systemHealth(ctrl.signal).then(setSysHealth).catch(() => {}),
    ]).catch(() => {});
    
    // Analytics probe
    api.analytics(ctrl.signal)
      .then(setData)
      .catch((err) => {
        setAnalyticsError(errMessage(err));
      });
    
    return () => ctrl.abort();
  }, []);

  // ── Charts lifecycle: create once the data + canvases exist, destroy on
  //    unmount. The canvases are only in the DOM after `data` arrives, and
  //    Preact runs effects after committing that render, so the refs are set. ──
  useEffect(() => {
    if (!data) return;
    const ec = eventsCanvasRef.current;
    const tc = tripsCanvasRef.current;
    if (!ec || !tc) return;
    const charts = new AnalyticsCharts({ events: ec, trips: tc }, buildId());
    chartsRef.current = charts;
    charts.render(toChartModel(data));
    return () => {
      charts.destroy();
      chartsRef.current = null;
    };
  }, [data]);

  // Storage data setup
  const filesystems = info?.filesystems ?? [];
  const primary = primaryFs(filesystems);
  const volumes = info?.volumes ?? [];
  const dashcam = pickVolume(volumes, "dashcam");
  const media = pickVolume(volumes, "media");
  
  // Storage-relevant subsystems lifted from /api/system/health
  const STORAGE_SUBSYSTEMS = [
    { key: "disk", label: "SD Card" },
    { key: "storage_writable", label: "Storage Roots" },
    { key: "gadget", label: "USB Gadget" },
  ];

  const load = metrics?.load ?? null;
  const mem = memTile(metrics?.mem ?? null, "");
  const swap = memTile(metrics?.swap ?? null, "none");

  // Analytics data setup
  const miles =
    data != null
      ? `${(data.total_distance_m / METERS_PER_MILE).toFixed(1)} mi`
      : EM_DASH;
  const trips = data != null ? String(data.total_trips) : EM_DASH;
  const events = data != null ? String(data.total_events) : EM_DASH;
  const driveTime =
    data?.total_drive_time_s != null
      ? formatDuration(data.total_drive_time_s)
      : EM_DASH;
  const avgSpeed =
    data?.avg_speed_mps != null ? `${mph(data.avg_speed_mps)} mph` : EM_DASH;
  const maxSpeed =
    data?.max_speed_mps != null ? `${mph(data.max_speed_mps)} mph` : EM_DASH;
  const warnCount =
    data?.warning_event_count != null
      ? String(data.warning_event_count)
      : EM_DASH;
  const evPer100 =
    data != null && data.total_distance_m > 0
      ? ((data.total_events * 100 * METERS_PER_MILE) / data.total_distance_m).toFixed(1)
      : EM_DASH;
  const vs = data?.video_stats ?? null;

  return (
    <div
      class="storage-page"
      id="analyticsDashboard"
      data-page="analytics"
      data-screen="analytics"
    >
      <h2 class="storage-title">
        <Icon name="bar-chart-2" /> Storage Analytics Dashboard
      </h2>

      {/* ── STORAGE SECTION (top) ── */}
      <EncryptionBanner enc={enc} />
      <RecordingBanner primary={primary} governor={info?.governor ?? null} />

      <SdCompositionCard primary={primary} dashcam={dashcam} media={media} />

      <div class="cap-card-grid">
        <VolumeCard
          id="storage-dashcam-card"
          icon="video"
          title="TeslaCam"
          subtitle="The drive Tesla saves dashcam and Sentry clips to. Free space is measured exactly from the drive's own allocation map."
          volume={dashcam}
        />
        <VolumeCard
          id="storage-media-card"
          icon="music"
          title="Media"
          subtitle="Holds Boombox, Light Show and other media you load for the car. Reported by the filesystem."
          volume={media}
        />
      </div>

      <details class="storage-details" id="storage-device-health">
        <summary class="storage-details-summary">
          <Icon name="settings" /> Device health &amp; diagnostics
        </summary>
        <div class="storage-details-body">

        {/* Storage Health — /api/storage/health. Wear-telemetry rows degrade. */}
        <section class="storage-card" id="storage-health-card">
          <div class="storage-card-head">
            <h2 class="storage-card-title">
              <Icon name="hard-drive" /> Storage health
            </h2>
            <SeverityBadge severity={health?.severity} />
          </div>
          <p class="storage-help" id="storage-health-summary">
            {health?.summary ?? "Storage health is being probed…"}
          </p>
          <dl class="storage-dl" id="storage-health-grid">
            <dt>Device</dt>
            <dd>{health?.device ?? DASH}</dd>
            <dt>Filesystem</dt>
            <dd>{health?.fstype ?? DASH}</dd>
            <dt>Mount</dt>
            <dd>{health?.mount ?? DASH}</dd>
            <dt>Used</dt>
            <dd>{humanBytesBinary(health?.used_bytes)}</dd>
            <dt>Total</dt>
            <dd>{humanBytesBinary(health?.total_bytes)}</dd>
            <dt>Filesystem errors</dt>
            <dd>{health?.fs_errors != null ? String(health.fs_errors) : DASH}</dd>
            <dt>TRIM</dt>
            <dd>{health?.trim ?? DASH}</dd>
          </dl>
        </section>

        {/* Filesystems — one live used/free bar per mounted filesystem. */}
        <section class="storage-card" id="filesystems-card">
          <div class="storage-card-head">
            <h2 class="storage-card-title">
              <Icon name="hard-drive" /> Filesystems
            </h2>
            <span class="storage-pill storage-pill-muted">
              {filesystems.length} mounted
            </span>
          </div>
          {filesystems.length > 0 ? (
            <div class="fs-list" id="filesystems-list">
              {filesystems.map((fs) => (
                <FilesystemRow key={`${fs.device}:${fs.mount}`} fs={fs} />
              ))}
            </div>
          ) : (
            <p class="storage-note" data-testid="filesystems-degraded">
              No filesystems reported — the storage probe returned no mounts.
            </p>
          )}
        </section>

        {/* Subsystem health — storage-relevant rows from /api/system/health. */}
        <section class="storage-card" id="storage-subsystems-card">
          <h2 class="storage-card-title">
            <Icon name="shield" /> Subsystem status
          </h2>
          <dl class="storage-dl" id="storage-subsystems-grid">
            {STORAGE_SUBSYSTEMS.map((sub) => {
              const block = sysHealth?.subsystems?.[sub.key] ?? null;
              const key = sevKey(block?.severity);
              return (
                <Fragment key={sub.key}>
                  <dt>
                    <span
                      class="storage-dot"
                      aria-label={key}
                      style={`background:${SEV_COLORS[key]}; margin-right:6px; vertical-align:middle;`}
                    />
                    {sub.label}
                  </dt>
                  <dd>{block?.message ?? DASH}</dd>
                </Fragment>
              );
            })}
          </dl>
        </section>

        {/* Live resources — /api/system/metrics. Unreadable tiles stay "—". */}
        <section class="storage-card" id="storage-resources-card">
          <h2 class="storage-card-title">
            <Icon name="zap" /> Live resources
          </h2>
          <div class="storage-metrics">
            <div class="storage-metric" id="storage-metric-mem">
              <span class="storage-metric-label">Memory</span>
              <span class="storage-metric-value">{mem.value}</span>
              <span class="storage-metric-detail">{mem.detail || "\u00a0"}</span>
            </div>
            <div class="storage-metric" id="storage-metric-swap">
              <span class="storage-metric-label">Swap</span>
              <span class="storage-metric-value">{swap.value}</span>
              <span class="storage-metric-detail">{swap.detail || "\u00a0"}</span>
            </div>
            <div class="storage-metric" id="storage-metric-load">
              <span class="storage-metric-label">Load (1m / 5m / 15m)</span>
              <span class="storage-metric-value">
                {load
                  ? `${load.one.toFixed(2)} / ${load.five.toFixed(2)} / ${load.fifteen.toFixed(2)}`
                  : DASH}
              </span>
              <span class="storage-metric-detail">{"\u00a0"}</span>
            </div>
            <div class="storage-metric" id="storage-metric-temp">
              <span class="storage-metric-label">CPU temperature</span>
              <span class="storage-metric-value">{formatTemp(metrics?.cpu_temp_c)}</span>
              <span class="storage-metric-detail">{tempBand(metrics?.cpu_temp_c)}</span>
            </div>
            <div class="storage-metric" id="storage-metric-uptime">
              <span class="storage-metric-label">Uptime</span>
              <span class="storage-metric-value">{formatUptime(metrics?.uptime_s)}</span>
              <span class="storage-metric-detail" id="storage-metric-updated">
                Updated {formatUpdated(metrics?.updated_at)}
              </span>
            </div>
          </div>
        </section>

        {/* Retention headroom — governor is null when retentiond is not reporting. */}
        <section class="storage-card" id="storage-retention-card">
          <h2 class="storage-card-title">
            <Icon name="database" /> Retention headroom
          </h2>
          {info?.governor != null ? (
            (() => {
              const g = info.governor;
              const isDryRun = g.mode !== "armed";
              const usedPct =
                g.total_bytes > 0
                  ? clampPct(((g.total_bytes - g.free_bytes) / g.total_bytes) * 100)
                  : 0;
              const freePct = g.total_bytes > 0 ? (g.free_bytes / g.total_bytes) * 100 : null;
              return (
                <div id="storage-governor">
                  <p class="storage-note" data-testid="governor-mode">
                    Auto-cleanup:{" "}
                    {g.mode === "armed" ? "Armed" : "Dry-run (reporting only)"}
                  </p>
                  <div
                    class="cap-bar"
                    role="img"
                    aria-label={`Retention governor: ${usedPct.toFixed(1)}% used (${humanBytesBinary(g.total_bytes - g.free_bytes)} of ${humanBytesBinary(g.total_bytes)})`}
                  >
                    <div class={`cap-seg ${capClass(usedPct / 100)}`} style={`width:${usedPct.toFixed(2)}%`} />
                    <div class="cap-seg cap-seg-free" style="flex:1" />
                  </div>
                  <p class="storage-note">
                    {humanBytesBinary(g.free_bytes)} {isDryRun ? "projected free" : "free"} of{" "}
                    {humanBytesBinary(g.total_bytes)} (
                    <span data-testid="governor-free-pct">
                      {freePct == null ? DASH : `${freePct.toFixed(1)}%`}
                    </span>
                    )
                  </p>
                  <p class="storage-note" data-testid="governor-target">
                    Keeps {"\u2265"} {(g.target_exit_frac * 100).toFixed(0)}% free
                  </p>
                  <p class="storage-note" data-testid="governor-last">
                    Last pass: {isDryRun ? "would free" : "freed"} {humanBytesBinary(g.last_bytes_freed)} ·{" "}
                    {g.last_items} clips · {GOV_STOP_LABEL[g.last_stop] ?? g.last_stop}
                  </p>
                  <p class="storage-note">
                    Protects footage newer than {formatProtectWindow(g.recency_floor_secs)}
                  </p>
                  <p class="storage-note">Updated {formatUpdated(g.updated_at)}</p>
                </div>
              );
            })()
          ) : (
            <p class="storage-note" data-testid="retention-degraded">
              The auto-cleanup governor is not reporting yet, so eviction headroom
              is unavailable. TeslaCam retention is managed on the device; this
              read-only view will surface live headroom once the governor is
              wired in.
            </p>
          )}
          {info?.quarantined != null && (
            <p class="storage-note" data-testid="quarantined-data">
              {info.quarantined.count === 0
                ? "No quarantined clips."
                : `Quarantined (never auto-deleted): ${info.quarantined.count} ${
                    info.quarantined.count === 1 ? "clip" : "clips"
                  } \u00b7 ${humanBytesBinary(info.quarantined.bytes)}`}
            </p>
          )}
        </section>
        </div>
      </details>

      {/* ── Horizontal divider between Storage and Analytics sections ── */}
      <hr style="margin: 2rem 0; border: none; border-top: 1px solid var(--border-card, #e5e7eb);" />

      {/* ── ANALYTICS SECTION (bottom) ── */}
      {analyticsError ? (
        <div class="alert alert-warning" role="status" data-testid="analytics-unavailable">
          <strong>
            <Icon name="alert-triangle" /> Analytics temporarily unavailable
          </strong>
          <div>
            The mapping database could not be read. The dashboard will recover
            automatically once the indexer is healthy.
          </div>
        </div>
      ) : (
        <>
          {/* Driving Statistics — live aggregates from /api/analytics; the
              speed/FSD/drive-time fields webd does not serve show the legacy "—". */}
          <div class="analytics-section" id="drivingStatsSection">
            <h3>
              <Icon name="map-pin" /> Driving Statistics
            </h3>
            <p class="section-description">
              GPS and telemetry derived from indexed dashcam clips.{" "}
              <a href="/">Open Map →</a>
            </p>
            <div id="drivingStatsContent">
              <div
                class="analytics-grid"
                id="drivingStatsGrid"
                data-testid="driving-stats"
              >
                <div class="analytics-card">
                  <div class="stat-row">
                    <span class="stat-label">Total Distance</span>
                    <span class="stat-value" id="dsTotalDist">
                      {miles}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Total Drive Time</span>
                    <span class="stat-value" id="dsTotalTime">
                      {driveTime}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Total Trips</span>
                    <span class="stat-value" id="dsTripCount">
                      {trips}
                    </span>
                  </div>
                </div>
                <div class="analytics-card">
                  <div class="stat-row">
                    <span class="stat-label">Avg Speed</span>
                    <span class="stat-value" id="dsAvgSpeed">
                      {avgSpeed}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Max Speed</span>
                    <span class="stat-value" id="dsMaxSpeed">
                      {maxSpeed}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">FSD Usage</span>
                    <span class="stat-value" id="dsFsdPct">
                      {EM_DASH}
                    </span>
                  </div>
                </div>
                <div class="analytics-card">
                  <div class="stat-row">
                    <span class="stat-label">Total Events</span>
                    <span class="stat-value" id="dsEventCount">
                      {events}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Warnings/Critical</span>
                    <span class="stat-value" id="dsWarnCount">
                      {warnCount}
                    </span>
                  </div>
                  <div class="stat-row">
                    <span class="stat-label">Events per 100 mi</span>
                    <span class="stat-value" id="dsEvPer100">
                      {evPer100}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Video Statistics — live footage aggregates from /api/analytics
              (catalog `clips`⋈`angles`, derived from indexed size_bytes). Uses
              the legacy analytics totals/table/folder-card styling for parity.
              Renders only when the webd build serves `video_stats`. */}
          {vs != null && (
            <div class="analytics-section" id="videoStatsSection">
              <h3>
                <Icon name="video" /> Video Statistics
              </h3>
              <p class="section-description">
                Footage indexed across all camera angles.
              </p>
              <div class="totals-row" data-testid="video-totals">
                <div class="total-stat">
                  <div class="total-number" id="vsTotalClips">
                    {vs.total_clips}
                  </div>
                  <div class="total-label">Clips</div>
                </div>
                <div class="total-stat">
                  <div class="total-number" id="vsTotalFiles">
                    {vs.total_files}
                  </div>
                  <div class="total-label">Camera Files</div>
                </div>
                <div class="total-stat">
                  <div class="total-number" id="vsTotalBytes">
                    {humanBytes(vs.total_bytes)}
                  </div>
                  <div class="total-label">Total Size</div>
                </div>
              </div>

              {/* Desktop: table. Mobile: cards. (Toggled by analytics.css.) */}
              <div class="folder-table-container">
                <table class="analytics-table" data-testid="folder-table">
                  <thead>
                    <tr>
                      <th>Folder</th>
                      <th class="number-cell">Clips</th>
                      <th class="number-cell">Files</th>
                      <th class="number-cell">Size</th>
                    </tr>
                  </thead>
                  <tbody>
                    {vs.by_folder_class.map((f) => (
                      <tr key={f.folder_class} data-folder={f.folder_class}>
                        <td>
                          <Icon name="folder" /> {folderLabel(f.folder_class)}
                        </td>
                        <td class="number-cell">{f.clip_count}</td>
                        <td class="number-cell">{f.file_count}</td>
                        <td class="number-cell">{humanBytes(f.size_bytes)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div class="folder-cards-mobile">
                {vs.by_folder_class.map((f) => (
                  <div class="folder-card" key={f.folder_class} data-folder={f.folder_class}>
                    <div class="folder-card-header">
                      <span class="folder-icon-large">
                        <Icon name="folder" />
                      </span>
                      <span>{folderLabel(f.folder_class)}</span>
                    </div>
                    <div class="folder-card-stats">
                      <div class="folder-stat">
                        <span class="folder-stat-label">Clips</span>
                        <span class="folder-stat-value">{f.clip_count}</span>
                      </div>
                      <div class="folder-stat">
                        <span class="folder-stat-label">Files</span>
                        <span class="folder-stat-value">{f.file_count}</span>
                      </div>
                      <div class="folder-stat">
                        <span class="folder-stat-label">Size</span>
                        <span class="folder-stat-value">
                          {humanBytes(f.size_bytes)}
                        </span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Charts — net-new, live from /api/analytics. Canvases mount only
              once data has arrived so Chart.js always has real datasets. */}
          <div class="analytics-section" id="analyticsChartsSection">
            <h3>
              <Icon name="bar-chart-2" /> Event &amp; Trip Charts
            </h3>
            {data == null ? (
              <p class="section-description" data-testid="charts-loading">
                Loading analytics…
              </p>
            ) : (
              <div class="analytics-charts">
                <div class="analytics-card chart-card">
                  <h4>Events by Type</h4>
                  <div class="chart-canvas-wrap">
                    <canvas id="eventsByTypeChart" ref={eventsCanvasRef} />
                  </div>
                </div>
                <div class="analytics-card chart-card">
                  <h4>Trips by Day</h4>
                  <div class="chart-canvas-wrap">
                    <canvas id="tripsByDayChart" ref={tripsCanvasRef} />
                  </div>
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
