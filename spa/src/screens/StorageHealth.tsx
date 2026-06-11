import { useEffect, useState } from "preact/hooks";
import { Fragment } from "preact";
import { Icon } from "../components/Icon";
import { api } from "../api/client";
import type {
  FilesystemEntry,
  StorageHealth as StorageHealthDto,
  StorageInfo,
  SystemHealth,
  SystemMetrics,
} from "../api/types";
import "../styles/storage.css";

/**
 * Storage health screen (route `/storage`, Shell active "settings") — a
 * read-only reproduction of the legacy Flask "Storage" page captured at
 * docs/tasks/parity-baseline/storage/, carrying its `storage.css` (scoped) so
 * the cards / pills / capacity-bar look land.
 *
 * Data boundary (why this is NOT a 1:1 carry of the legacy settings FORM):
 * webd's API is strictly READ-ONLY — it serves device/storage status probes
 * (`/api/storage`, `/api/storage/health`, `/api/system/metrics`,
 * `/api/system/health`) but exposes NO mutation endpoint and NO partition
 * allocation / OS-reserve / auto-cleanup config. So, exactly as the sibling
 * MediaHub and Analytics screens do, this screen renders the LIVE read-only
 * facts and DEGRADES anything webd cannot honestly serve to the legacy grey
 * "—" state rather than fabricating values or building a POST form:
 *   · Header pills — SD total / SD free / Used are live from /api/storage;
 *     Allocated / OS+reserve / Unallocated stay "—" (allocation data is not
 *     exposed by the read-only API — never fabricated).
 *   · Storage Health — severity/summary + device/fstype/mount/used/total from
 *     /api/storage/health; the SD wear-telemetry rows (fs errors, I/O errors,
 *     TRIM) stay "—" (SD cards expose none).
 *   · Filesystems — one live used/free bar per mounted filesystem.
 *   · Live resources — memory/swap/load/uptime from /api/system/metrics.
 *   · Retention headroom — `governor` is null until retentiond is wired in, so
 *     a degraded note is shown instead of a fabricated eviction figure.
 *
 * Every handler self-degrades to unknown/null and never 5xx, and each section
 * fetches independently, so a single probe blip leaves only that card in its
 * degraded state without logging (the zero-console UAT gate holds).
 */

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

/** A used/total memory or swap tile, degrading honestly when unreadable. */
function memTile(
  m: { total_bytes: number; available_bytes: number; used_pct: number } | null,
  emptyDetail: string,
): { value: string; detail: string } {
  if (!m || m.total_bytes <= 0) return { value: DASH, detail: emptyDetail };
  const used = Math.max(0, m.total_bytes - m.available_bytes);
  return {
    value: `${Math.round(clampPct(m.used_pct))}%`,
    detail: `${humanBytes(used)} / ${humanBytes(m.total_bytes)}`,
  };
}

/** Bytes → a compact human string (GB above 1 GiB, else MB). */
function humanBytes(n: number | null | undefined): string {
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
          {humanBytes(usedBytes)} / {humanBytes(fs.total_bytes)} used ({pctText})
        </span>
      </div>
      <div
        class="cap-bar"
        role="img"
        aria-label={`${fs.mount}: ${pctText} used (${humanBytes(usedBytes)} of ${humanBytes(fs.total_bytes)})`}
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
        {fs.device || DASH} · {fs.fstype || DASH} · {humanBytes(fs.free_bytes)} free
      </div>
    </div>
  );
}

export function StorageHealth() {
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [health, setHealth] = useState<StorageHealthDto | null>(null);
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);
  const [sysHealth, setSysHealth] = useState<SystemHealth | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    // Each read-only probe is fetched independently and self-degrades: a blip
    // leaves only that card unknown without logging (zero-console gate).
    api.storage(ctrl.signal).then(setInfo).catch(() => {});
    api.storageHealth(ctrl.signal).then(setHealth).catch(() => {});
    api.systemMetrics(ctrl.signal).then(setMetrics).catch(() => {});
    api.systemHealth(ctrl.signal).then(setSysHealth).catch(() => {});
    return () => ctrl.abort();
  }, []);

  const filesystems = info?.filesystems ?? [];
  const primary = primaryFs(filesystems);
  const primaryFrac =
    primary != null ? usedFraction(primary.total_bytes, primary.free_bytes) : null;
  const primaryUsed =
    primary != null && primaryFrac != null
      ? Math.max(0, primary.total_bytes - primary.free_bytes)
      : null;

  // Storage-relevant subsystems lifted from /api/system/health (degrade to "—").
  const STORAGE_SUBSYSTEMS = [
    { key: "disk", label: "SD Card" },
    { key: "storage_writable", label: "Storage Roots" },
    { key: "teslafat_0", label: "TeslaCam (exFAT)" },
    { key: "teslafat_1", label: "Media (exFAT)" },
    { key: "gadget", label: "USB Gadget" },
  ];

  const load = metrics?.load ?? null;
  const mem = memTile(metrics?.mem ?? null, "");
  const swap = memTile(metrics?.swap ?? null, "none");

  return (
    <section class="storage-page container" data-screen="storage-health">
      <header class="storage-header">
        <h1 class="storage-title">Storage</h1>
        <p class="storage-copy">
          Live, read-only view of the device's storage. The gadget exposes the
          SD card as USB media to Tesla; this page reports measured capacity,
          filesystem health, and live system resources. Allocation and
          auto-cleanup tuning are managed on the device — the read-only catalog
          API does not expose those controls, so allocation figures below show
          {" "}
          {DASH} until a writable control plane is wired in.
        </p>
        <div class="storage-pill-row">
          <span class="storage-pill">
            SD total: {humanBytes(primary?.total_bytes)}
          </span>
          <span class="storage-pill">
            SD free: {humanBytes(primary?.free_bytes)}
          </span>
          <span class="storage-pill">Used: {humanBytes(primaryUsed)}</span>
          {/* Allocation breakdown is not exposed by the read-only API — shown
              as "—" rather than fabricated (parity-degrade). */}
          <span class="storage-pill storage-pill-muted">Allocated: {DASH}</span>
          <span class="storage-pill storage-pill-muted">OS + reserve: {DASH}</span>
          <span class="storage-pill storage-pill-muted">Unallocated: {DASH}</span>
        </div>

        {primary != null && primaryFrac != null ? (
          <>
            <div
              class="cap-bar"
              role="img"
              aria-label={`Primary volume ${primary.mount}: ${Math.round(primaryFrac * 100)}% used (${humanBytes(primaryUsed)} of ${humanBytes(primary.total_bytes)})`}
            >
              <div
                class={`cap-seg ${capClass(primaryFrac)}`}
                style={`width:${(primaryFrac * 100).toFixed(2)}%`}
                data-primary-used
              />
              <div class="cap-seg cap-seg-free" style="flex:1" />
            </div>
            <div class="cap-legend">
              <span>
                <i class={`cap-swatch ${capClass(primaryFrac)}`} />
                Used {humanBytes(primaryUsed)} on {primary.mount}
              </span>
              <span>
                <i
                  class="cap-swatch cap-seg-free"
                  style="border:1px solid var(--border-input)"
                />
                Free {humanBytes(primary.free_bytes)}
              </span>
            </div>
          </>
        ) : (
          <p class="storage-note" data-testid="storage-capacity-degraded">
            Capacity is unavailable — no readable filesystem was reported.
          </p>
        )}
      </header>

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
          <dd>{humanBytes(health?.used_bytes)}</dd>
          <dt>Total</dt>
          <dd>{humanBytes(health?.total_bytes)}</dd>
          <dt>Filesystem errors</dt>
          <dd>{health?.fs_errors != null ? String(health.fs_errors) : DASH}</dd>
          <dt>I/O errors (24h)</dt>
          <dd>{health?.io_errors_24h != null ? String(health.io_errors_24h) : DASH}</dd>
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

      {/* Retention headroom — governor is null until retentiond lands. */}
      <section class="storage-card" id="storage-retention-card">
        <h2 class="storage-card-title">
          <Icon name="database" /> Retention headroom
        </h2>
        {info?.governor != null ? (
          <pre class="storage-note" id="storage-governor">
            {JSON.stringify(info.governor, null, 2)}
          </pre>
        ) : (
          <p class="storage-note" data-testid="retention-degraded">
            The auto-cleanup governor is not reporting yet, so eviction headroom
            is unavailable. TeslaCam retention is managed on the device; this
            read-only view will surface live headroom once the governor is
            wired in.
          </p>
        )}
      </section>
    </section>
  );
}
