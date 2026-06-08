import { useEffect, useState } from "preact/hooks";
import { Fragment } from "preact";
import { api } from "../api/client";
import type { Pref } from "../api/types";

/**
 * Home screen (Task 5.2) — a faithful visual + structural reproduction of the
 * legacy Flask settings / device-status dashboard (`index.html`), the page
 * captured as the parity baseline at `docs/tasks/parity-baseline/media-hub/`.
 *
 * Read-only by construction. webd's catalog API does not expose the legacy
 * system services (`/api/system/health`, `/api/system/metrics`,
 * `/api/storage/health`) nor any mutation route, so the device-status, System
 * Health, Live Metrics and Storage Health sections render the legacy template's
 * **degraded / loading / unknown** initial state verbatim — never calling the
 * absent endpoints (which would 404 and break the zero-console UAT gate). This
 * is the parity the integrator endorsed: "a catalog-only webd organically
 * reproduces that [degraded] look — that IS parity". The one live read the
 * screen performs is `GET /api/settings`, used to populate the config-form
 * fields where prefs keys map. Real CPU/MEM live metrics require a future
 * system-metrics endpoint (tracked gap) — the tiles show the "—" zero-state
 * rather than fabricated numbers.
 *
 * The config forms (Mapping & Indexing, Network File Sharing) are reproduced
 * for structural parity but are inert: buttons are `type="button"` and the
 * forms `preventDefault`, so the screen can never issue a mutation.
 */

function pref(prefs: Pref[] | null, key: string, dflt = ""): string {
  const p = prefs?.find((x) => x.key === key);
  return p ? p.value : dflt;
}

function prefBool(prefs: Pref[] | null, key: string): boolean {
  const v = pref(prefs, key).toLowerCase();
  return v === "1" || v === "true" || v === "on" || v === "yes";
}

const METRIC_TILES = [
  { id: "metric-load", label: "Load (1m / 5m / 15m)" },
  { id: "metric-cpu", label: "CPU" },
  { id: "metric-mem", label: "Memory" },
  { id: "metric-swap", label: "Swap" },
  { id: "metric-sd", label: "SD Card I/O" },
  { id: "metric-usb", label: "USB I/O (nbd0)" },
];

const TIMEZONES = [
  "UTC",
  "America/Los_Angeles",
  "America/Denver",
  "America/Chicago",
  "America/New_York",
  "Europe/London",
  "Europe/Berlin",
];

// System Health subsystems + severity colors — transcribed verbatim from the
// legacy index.html card (Phase 4.2). The legacy JS renders any subsystem key
// absent from the probe payload as a grey "unknown" dot + "—" message; we feed
// a payload where ONLY `indexer` (Video Indexer) is populated, derived from the
// read-only catalog, so every other (system-probe) row degrades to that legacy
// "—" state rather than a fabricated value.
const SUBSYSTEMS = [
  { key: "gadget", label: "USB Gadget" },
  { key: "teslafat_0", label: "TeslaCam (exFAT)" },
  { key: "teslafat_1", label: "Media (exFAT)" },
  { key: "worker", label: "Background Worker" },
  { key: "indexer", label: "Video Indexer" },
  { key: "disk", label: "SD Card" },
  { key: "storage_writable", label: "Storage Roots" },
  { key: "network", label: "WiFi" },
  { key: "samba", label: "Network Share" },
  { key: "journal", label: "Recent Errors" },
];

const SEV_COLORS: Record<string, string> = {
  ok: "var(--accent-success, #4caf50)",
  warn: "var(--accent-warning, #ff9800)",
  error: "var(--accent-error,   #f44336)",
  unknown: "var(--text-secondary, #888)",
};

interface HealthBlock {
  severity: string;
  message: string;
}

export function MediaHub() {
  const [prefs, setPrefs] = useState<Pref[] | null>(null);
  const [indexer, setIndexer] = useState<HealthBlock | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    api
      .settings(ctrl.signal)
      .then(setPrefs)
      .catch(() => {
        // Read-only degrade: fall back to template defaults without logging,
        // so an absent/empty prefs store never trips the zero-console gate.
      });
    // Video Indexer status is the one System Health row that IS catalog data:
    // clip count + newest-clip age, derived from the read-only catalog. Every
    // other subsystem stays in the legacy "—" unknown state (not fabricated).
    api
      .clips({ limit: 500 }, ctrl.signal)
      .then((page) => {
        const clips = page.items;
        const count = clips.length;
        if (count === 0) {
          setIndexer({ severity: "unknown", message: "0 clips indexed" });
          return;
        }
        const newest = Math.max(...clips.map((c) => c.started_at));
        const daysOld = Math.floor((Date.now() / 1000 - newest) / 86400);
        setIndexer({
          severity: "ok",
          message: `${count} clips indexed; newest is ${daysOld} d old`,
        });
      })
      .catch(() => {
        // Degrade: the Video Indexer row stays "—"/unknown like the others.
      });
    return () => ctrl.abort();
  }, []);

  return (
    // Bare screen content — the router hoists a single shared <Shell> and
    // supplies the active nav key (`/media` → "media"), so this screen no
    // longer wraps itself in Shell (was <Shell active="settings"> in the
    // standalone 5.2 build before the 5.3 router landed).
    <div class="container" data-screen="settings-dashboard">
        {/* Device Status — degraded "unknown" variant (exact baseline). */}
        <div class="device-status-card device-status-unknown">
          <div class="device-status-header">
            <span class="status-dot status-unknown" />
            <div class="device-status-info">
              <strong>Status Unknown</strong>
              <p>Unable to determine current device status.</p>
            </div>
          </div>
        </div>

        {/* System Health — the legacy probe (/api/system/health) is NOT part of
            the read-only catalog API, so each subsystem row degrades to the
            legacy "unknown / —" state EXCEPT Video Indexer, which is derived
            from the read-only catalog (clip count + newest age) — exactly the
            baseline's "N clips indexed; newest is M d old". No system metric is
            fabricated; the overall stays the legacy degraded default. */}
        <details class="settings-section" id="system-health-section" open>
          <summary>System Health</summary>
          <div class="section-content">
            <div
              id="system-health-card"
              style="display:flex; flex-direction:column; gap:6px;"
            >
              <p
                id="system-health-overall"
                style="margin:0 0 6px; padding:8px 12px; border-radius:8px; font-size:0.95rem; display:flex; align-items:center; gap:8px; background:var(--bg-secondary); border:1px solid var(--border-color);"
              >
                <span
                  class="health-dot health-dot-unknown"
                  aria-hidden="true"
                  style="width:10px; height:10px; border-radius:50%; display:inline-block; flex-shrink:0; background:var(--text-secondary, #888);"
                />
                <span id="system-health-overall-text">Unknown</span>
              </p>
              <div
                id="system-health-rows"
                style="display:grid; grid-template-columns:auto auto 1fr; gap:6px 12px; align-items:center; font-size:0.9rem;"
              >
                {SUBSYSTEMS.map((sub) => {
                  const block = sub.key === "indexer" ? indexer : null;
                  const sev = block?.severity ?? "unknown";
                  const msg = block?.message ?? "—";
                  return (
                    <Fragment key={sub.key}>
                      <div>
                        <span
                          aria-label={sev}
                          style={`width:10px; height:10px; border-radius:50%; display:inline-block; flex-shrink:0; background:${SEV_COLORS[sev] ?? SEV_COLORS.unknown};`}
                        />
                      </div>
                      <div style="color:var(--text-primary)">{sub.label}</div>
                      <div style="color:var(--text-secondary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
                        {msg}
                      </div>
                    </Fragment>
                  );
                })}
              </div>
            </div>
          </div>
        </details>

        {/* Live Metrics — zero-state tiles (system-metrics endpoint is a tracked
            gap; we do not fabricate CPU/MEM numbers). */}
        <details class="settings-section" id="live-metrics-section" open>
          <summary>Live Metrics</summary>
          <div class="section-content">
            <div
              id="live-metrics-card"
              style="display:flex; flex-direction:column; gap:8px;"
            >
              <div
                id="live-metrics-grid"
                style="display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:10px;"
              >
                {METRIC_TILES.map((t) => (
                  <div class="metric-tile" id={t.id} key={t.id}>
                    <div class="metric-label">{t.label}</div>
                    <div class="metric-value">—</div>
                    <div class="metric-detail">&nbsp;</div>
                  </div>
                ))}
              </div>
              <p
                id="live-metrics-foot"
                style="margin:0; font-size:0.78rem; color:var(--text-secondary);"
              >
                Updated <span id="live-metrics-updated">—</span> ·{" "}
                <span id="live-metrics-uptime">—</span>
              </p>
            </div>
          </div>
        </details>

        {/* WiFi Networks — collapsed; inert (no live nmcli probe). */}
        <details class="settings-section">
          <summary>WiFi Networks</summary>
          <div class="section-content">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
              <p style="font-size:0.85em;color:var(--text-secondary);margin:0">
                Networks higher in the list are preferred when multiple are in
                range.
              </p>
              <button
                type="button"
                class="edit-btn"
                id="btnWifiScan"
                style="padding:4px 10px;font-size:0.85em;white-space:nowrap"
                disabled
              >
                Scan
              </button>
            </div>
            <div id="savedNetworksList">
              <div style="text-align:center;padding:12px;color:var(--text-secondary)">
                Wi-Fi management is not available in the read-only catalog build.
              </div>
            </div>
          </div>
        </details>

        {/* Access Point — degraded (no Wi-Fi tooling), matching the baseline. */}
        <details class="settings-section">
          <summary>Access Point</summary>
          <div class="section-content">
            <div style="color: var(--error-text);">
              AP status unavailable (Wi-Fi tooling is not part of the read-only
              catalog build).
            </div>
          </div>
        </details>

        {/* Storage &amp; Auto-Cleanup — link card (parity). */}
        <details class="settings-section">
          <summary>Storage &amp; Auto-Cleanup</summary>
          <div class="section-content">
            <a
              href="/storage"
              class="action-card"
              style="display:flex; align-items:center; gap:12px; padding:14px; text-decoration:none; color:inherit; border:1px solid var(--border-color); border-radius:8px; min-height:44px;"
            >
              <svg class="inline-icon" width="24" height="24" aria-hidden="true">
                <use href="/static/icons/lucide-sprite.svg#icon-hard-drive" />
              </svg>
              <div style="flex:1">
                <strong>Storage Settings</strong>
                <p style="margin:4px 0 0; font-size:0.85rem; color:var(--text-secondary)">
                  Adjust USB drive sizes and tier-aware auto-cleanup for
                  TeslaCam.
                </p>
              </div>
              <svg
                class="inline-icon"
                width="20"
                height="20"
                aria-hidden="true"
                style="color:var(--text-secondary)"
              >
                <use href="/static/icons/lucide-sprite.svg#icon-chevron-right" />
              </svg>
            </a>
          </div>
        </details>

        {/* Mapping & Indexing — config form, inert; bound to /api/settings. */}
        <details class="settings-section">
          <summary>Mapping &amp; Indexing</summary>
          <div class="section-content">
            <form onSubmit={(e) => e.preventDefault()}>
              <p style="font-size:0.85rem; color:var(--text-secondary); margin:0 0 16px">
                Tesla embeds GPS coordinates and telemetry data (speed, braking,
                steering) inside each dashcam video. The indexer extracts this
                data and builds a database of trips, routes, and driving events.
              </p>
              <div class="settings-form-grid">
                <div class="form-group">
                  <label style="font-size:0.85rem">
                    <strong>Trip gap (minutes)</strong>
                  </label>
                  <input
                    type="number"
                    name="trip_gap_minutes"
                    value={pref(prefs, "trip_gap_minutes", "10")}
                    min="1"
                    max="60"
                    class="settings-form-input"
                  />
                </div>
                <div class="form-group">
                  <label for="mapping-speed-limit" style="font-size:0.85rem">
                    <strong>Speed alert (mph)</strong>
                  </label>
                  <input
                    id="mapping-speed-limit"
                    type="number"
                    name="speed_limit_mph"
                    value={pref(prefs, "speed_limit_mph", "85")}
                    min="0"
                    max="200"
                    step="5"
                    class="settings-form-input"
                  />
                </div>
                <div class="form-group">
                  <label for="mapping-speed-units" style="font-size:0.85rem">
                    <strong>Map speed display units</strong>
                  </label>
                  <select
                    id="mapping-speed-units"
                    name="speed_units"
                    class="settings-form-input"
                    value={pref(prefs, "speed_units", "mph")}
                  >
                    <option value="mph">mph</option>
                    <option value="kph">kph</option>
                  </select>
                </div>
                <div class="form-group">
                  <label
                    for="mapping-display-timezone"
                    style="font-size:0.85rem"
                  >
                    <strong>Map day timezone</strong>
                  </label>
                  <select
                    id="mapping-display-timezone"
                    name="display_timezone"
                    class="settings-form-input"
                    value={pref(prefs, "display_timezone")}
                  >
                    <option value="">Auto (use this device's timezone)</option>
                    {TIMEZONES.map((tz) => (
                      <option value={tz} key={tz}>
                        {tz}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <button
                type="button"
                class="btn btn-primary"
                style="width:100%"
                disabled
              >
                Save Mapping Settings
              </button>
            </form>
          </div>
        </details>

        {/* Network File Sharing — config form, inert; bound to /api/settings. */}
        <details class="settings-section" id="network-file-sharing">
          <summary>Network File Sharing</summary>
          <div class="section-content">
            <form onSubmit={(e) => e.preventDefault()}>
              <p style="font-size:0.85rem; color:var(--text-secondary); margin:0 0 12px">
                Enable Samba (SMB) network sharing so you can browse the TeslaCam
                and media partitions from your computer over WiFi.
              </p>
              <div
                class="form-group"
                style="margin-bottom:12px; padding:12px; background-color:var(--bg-info); border:1px solid var(--border-color); border-radius:var(--radius-md,8px)"
              >
                <label style="display:flex; align-items:center; gap:12px; font-size:0.95rem; min-height:44px; cursor:pointer; margin:0">
                  <input
                    type="checkbox"
                    name="samba_enabled"
                    id="samba_enabled"
                    value="on"
                    checked={prefBool(prefs, "samba_enabled")}
                    style="width:22px; height:22px; flex:0 0 22px"
                  />
                  <span>
                    <strong>Enable network file sharing (SMB)</strong>
                  </span>
                </label>
              </div>
              <div class="form-group" style="margin-bottom:12px">
                <label style="font-size:0.85rem">Samba Password</label>
                <div style="display:flex; gap:8px">
                  <input
                    type="password"
                    name="samba_password"
                    id="samba_password"
                    value=""
                    placeholder="Set a password before clients can connect"
                    class="settings-form-input"
                    style="flex:1"
                    autocomplete="new-password"
                  />
                </div>
                <p style="font-size:0.8rem; color:var(--text-secondary); margin:4px 0 0">
                  Username: <code>pi</code>
                </p>
              </div>
              <button
                type="button"
                class="btn btn-primary"
                style="width:100%"
                disabled
              >
                Save Network Settings
              </button>
            </form>
          </div>
        </details>

        {/* Storage Health — static "checking / —" skeleton (the live probe is
            not part of the read-only catalog API; tracked gap). */}
        <details class="settings-section" id="storage-health-section">
          <summary>Storage Health</summary>
          <div class="section-content" id="storage-health-card">
            <div class="storage-health-header">
              <span
                class="status-dot health-dot health-dot-unknown"
                id="storage-health-dot"
                aria-label="Storage health severity"
              />
              <strong id="storage-health-summary">Checking…</strong>
            </div>
            <dl class="storage-health-grid" id="storage-health-grid">
              <dt>Device</dt>
              <dd>—</dd>
              <dt>Filesystem</dt>
              <dd>—</dd>
              <dt>Mount</dt>
              <dd>—</dd>
              <dt>Filesystem errors</dt>
              <dd>—</dd>
              <dt>I/O errors (24 h)</dt>
              <dd>—</dd>
              <dt>TRIM</dt>
              <dd>—</dd>
            </dl>
            <p class="storage-health-footer">
              SD cards expose no wear telemetry (no SMART, no per-block
              checksums). Plan to replace the card every 12 months and keep cloud
              archive enabled so a card failure never costs you data.
            </p>
          </div>
        </details>

        {/* System — host facts are an on-device concern; rendered as unknown in
            the read-only catalog build rather than fabricated. */}
        <details class="settings-section">
          <summary>System</summary>
          <div class="section-content">
            <div style="display:grid; grid-template-columns:auto 1fr; gap:6px 16px; font-size:0.9rem;">
              <span style="color:var(--text-secondary)">Hostname</span>
              <strong>—</strong>
              <span style="color:var(--text-secondary)">IP Address</span>
              <span>—</span>
              <span style="color:var(--text-secondary)">Uptime</span>
              <span>—</span>
              <span style="color:var(--text-secondary)">Platform</span>
              <span>—</span>
              <span style="color:var(--text-secondary)">Memory</span>
              <span>—</span>
              <span style="color:var(--text-secondary)">Version</span>
              <code style="font-size:0.8rem">B-1</code>
            </div>
          </div>
        </details>
      </div>
  );
}
