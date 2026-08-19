import { useEffect, useRef, useState } from "preact/hooks";
import { Fragment } from "preact";
import { ApiError, api } from "../api/client";
import type {
  ApInfo,
  ApMode,
  FsckHistoryEntry,
  FsckLastCheckResponse,
  FsckStatusResponse,
  GadgetModeStatus,
  HealthBlock,
  Pref,
  SavedWifiNetwork,
  StorageHealth,
  SystemHealth,
  SystemMetrics,
  WifiNetwork,
  WifiStatus,
} from "../api/types";

/**
 * Home screen (Task 5.2) — a faithful visual + structural reproduction of the
 * legacy Flask settings / device-status dashboard (`index.html`), the page
 * captured as the parity baseline at `docs/tasks/parity-baseline/media-hub/`.
 *
 * Read-only by construction. The device-status, System Health, Live Metrics and
 * Storage Health sections are populated from webd's read-only probe endpoints
 * (`/api/system/health`, `/api/system/metrics`, `/api/storage/health`, added in
 * 5.1d). Those handlers never 5xx and degrade to `unknown`/null for any
 * subsystem webd cannot honestly observe (the governor tier, inactive services,
 * car-owned exFAT volumes, SD wear telemetry), so a row that has no real signal
 * renders the legacy template's grey "unknown / —" state verbatim rather than a
 * fabricated value — which is exactly the parity baseline. The Video Indexer row
 * is the one System Health entry derived client-side from the catalog (clip
 * count + newest-clip age). `GET /api/settings` still populates the config-form
 * fields. CPU and SD Card I/O now render sampled client-side deltas from raw
 * counters; the old USB I/O tile was removed because there is no honest
 * per-device USB counter separate from mmcblk0 writes.
 *
 * The Mapping & Indexing form is live and persists changed settings through
 * `PUT /api/settings` using canonical pref keys (`speed_unit`,
 * `trip_gap_minutes`, `speed_limit_mph`, `display_timezone`).
 */

function pref(prefs: Pref[] | null, key: string, dflt = ""): string {
  const p = prefs?.find((x) => x.key === key);
  return p ? p.value : dflt;
}

type MappingPrefs = {
  trip_gap_minutes: string;
  speed_limit_mph: string;
  speed_unit: string;
  display_timezone: string;
};

const DEFAULT_MAPPING_PREFS: MappingPrefs = {
  trip_gap_minutes: "5",
  speed_limit_mph: "80",
  speed_unit: "mph",
  display_timezone: "",
};

function mappingPrefsFromRows(prefs: Pref[] | null): MappingPrefs {
  return {
    trip_gap_minutes: pref(
      prefs,
      "trip_gap_minutes",
      DEFAULT_MAPPING_PREFS.trip_gap_minutes,
    ),
    speed_limit_mph: pref(
      prefs,
      "speed_limit_mph",
      DEFAULT_MAPPING_PREFS.speed_limit_mph,
    ),
    speed_unit: pref(prefs, "speed_unit", DEFAULT_MAPPING_PREFS.speed_unit),
    display_timezone: pref(
      prefs,
      "display_timezone",
      DEFAULT_MAPPING_PREFS.display_timezone,
    ),
  };
}

const METRIC_TILES = [
  { id: "metric-load", label: "Load (1m / 5m / 15m)" },
  { id: "metric-cpu", label: "CPU" },
  { id: "metric-temp", label: "CPU temp" },
  { id: "metric-mem", label: "Memory" },
  { id: "metric-swap", label: "Swap" },
  { id: "metric-sd", label: "SD Card I/O" },
];

const TIMEZONE_FMT = new Intl.DateTimeFormat("en-US", {
  timeZoneName: "shortOffset",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function timezoneOffsetLabel(_tz: string): string {
  try {
    TIMEZONE_FMT.resolvedOptions();
    const parts = TIMEZONE_FMT.formatToParts(new Date());
    const part = parts.find((p) => p.type === "timeZoneName")?.value ?? "";
    return part.startsWith("GMT") ? ` (${part})` : "";
  } catch {
    return "";
  }
}

function browserTimezones(): string[] {
  const values = Intl.supportedValuesOf?.("timeZone");
  if (Array.isArray(values) && values.length > 0) return values;
  return ["UTC"];
}

const TIMEZONES = browserTimezones();

// System Health subsystems + severity colors — transcribed verbatim from the
// legacy index.html card (Phase 4.2). The system-probe rows
// (gadget/worker/disk/storage_writable/network/journal) come from webd's
// `/api/system/health`; `indexer` (Video Indexer) is derived client-side from
// the catalog. Any subsystem key absent from the probe payload degrades to the
// legacy grey "unknown / —" state rather than a fabricated value.
const SUBSYSTEMS = [
  { key: "gadget", label: "USB Gadget" },
  { key: "worker", label: "Background Worker" },
  { key: "indexer", label: "Video Indexer" },
  { key: "disk", label: "SD Card" },
  { key: "storage_writable", label: "Storage Roots" },
  { key: "network", label: "WiFi" },
  { key: "journal", label: "Recent Errors" },
];

const SEV_COLORS: Record<string, string> = {
  ok: "var(--accent-success, #4caf50)",
  warn: "var(--accent-warning, #ff9800)",
  error: "var(--accent-error,   #f44336)",
  unknown: "var(--text-secondary, #888)",
};

// Overall-severity → the device-status banner copy + the System-Health rollup
// label. Mirrors the legacy template's wording; `unknown` keeps the baseline's
// "Status Unknown / Unable to determine…" degraded text.
const STATUS_COPY: Record<string, { title: string; detail: string }> = {
  ok: { title: "Online", detail: "All systems nominal." },
  warn: { title: "Degraded", detail: "One or more subsystems need attention." },
  error: { title: "Attention needed", detail: "A subsystem is reporting an error." },
  unknown: {
    title: "Status Unknown",
    detail: "Unable to determine current device status.",
  },
};

const OVERALL_LABEL: Record<string, string> = {
  ok: "Healthy",
  warn: "Degraded",
  error: "Attention needed",
  unknown: "Unknown",
};

/** Bytes → a compact human string (GB above 1 GiB, else MB). */
function humanBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const gib = n / 1024 ** 3;
  if (gib >= 1) return `${gib.toFixed(gib >= 10 ? 0 : 1)} GB`;
  return `${(n / 1024 ** 2).toFixed(0)} MB`;
}

/** Bytes/sec → compact rate string ("1.2 MB/s" / "512 KB/s" / "0 B/s"). */
function formatRate(bps: number | null): string {
  if (bps == null || !Number.isFinite(bps) || bps < 0) return "—";
  if (bps >= 1024 ** 2) return `${(bps / 1024 ** 2).toFixed(1)} MB/s`;
  if (bps >= 1024) return `${(bps / 1024).toFixed(0)} KB/s`;
  return `${Math.round(bps)} B/s`;
}

/** Seconds → "Xd Yh Zm" (drops leading zero units). */
function formatUptime(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s) || s < 0) return "—";
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
  if (epoch == null || !Number.isFinite(epoch)) return "—";
  return new Date(epoch * 1000).toLocaleTimeString();
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

function partitionLabel(partition: "part1" | "part2" | "part3"): string {
  if (partition === "part1") return "TeslaCam";
  if (partition === "part2") return "Light Show";
  return "Music";
}

function fsckResultLabel(result: string | null | undefined): string {
  if (!result) return "—";
  return result.replace(/_/g, " ");
}

function formatLastCheckAge(ageHours: number | undefined): string {
  if (ageHours == null || !Number.isFinite(ageHours)) return "Unknown age";
  if (ageHours < 1) return "Less than 1 hour ago";
  if (ageHours < 24) return `${Math.round(ageHours)} hours ago`;
  return `${Math.round(ageHours / 24)} days ago`;
}

function gadgetModeLabel(mode: GadgetModeStatus["mode"] | undefined): string {
  if (mode === "syncing") return "Syncing";
  if (mode === "presented") return "Presented";
  if (mode === "degraded") return "Degraded";
  return "Unavailable";
}

function gadgetHandoffLabel(gadget: GadgetModeStatus): string {
  const bits = [gadget.handoff.state === "active" ? "Active" : "Idle"];
  if (
    gadget.handoff.pending_mutations != null &&
    gadget.handoff.applying_mutations != null
  ) {
    bits.push(
      `Queue ${gadget.handoff.pending_mutations} pending / ${gadget.handoff.applying_mutations} applying`,
    );
  }
  if (gadget.handoff.last_result) {
    bits.push(
      `Last ${gadget.handoff.last_result}${gadget.handoff.last_handoff_id ? ` (${gadget.handoff.last_handoff_id})` : ""}`,
    );
  }
  return bits.join(" · ");
}

function lunStatusLabel(image: string | null, loaded: boolean | null): string {
  const loadedText =
    loaded === null ? "Unknown" : loaded ? "Loaded" : "Not loaded";
  return image ? `${loadedText} (${image})` : loadedText;
}

function recoveryLabel(gadget: GadgetModeStatus): string {
  if (gadget.banner?.message) return gadget.banner.message;
  const reenum = gadget.recovery.last_reenum;
  if (reenum?.result) {
    if (reenum.disconnect_ms != null) {
      return `Last USB re-enumeration: ${reenum.result} (${reenum.disconnect_ms} ms disconnect)`;
    }
    return `Last USB re-enumeration: ${reenum.result}`;
  }
  return "—";
}

function wifiSignalBars(signal: number): string {
  if (signal >= 75) return "▂▄▆█";
  if (signal >= 50) return "▂▄▆";
  if (signal >= 25) return "▂▄";
  return "▂";
}

/** A used/total memory tile → `{value:"NN%", detail:"used / total"}`. */
function memTile(
  m: { total_bytes: number; available_bytes: number; used_pct: number } | null,
  emptyDetail: string,
): { value: string; detail: string } {
  if (!m || m.total_bytes <= 0) return { value: "—", detail: emptyDetail };
  const used = m.total_bytes - m.available_bytes;
  return {
    value: `${Math.round(m.used_pct)}%`,
    detail: `${humanBytes(used)} / ${humanBytes(m.total_bytes)}`,
  };
}

/** Per-tile value/detail from the metrics payload; unprobed tiles stay "—". */
function metricFor(
  id: string,
  m: SystemMetrics | null,
  derived: { cpuPct: number | null; sdReadBps: number | null; sdWriteBps: number | null },
): { value: string; detail: string } {
  if (!m) return { value: "—", detail: "" };
  switch (id) {
    case "metric-load":
      return m.load
        ? {
            value: `${m.load.one.toFixed(2)} / ${m.load.five.toFixed(2)} / ${m.load.fifteen.toFixed(2)}`,
            detail: "",
          }
        : { value: "—", detail: "" };
    case "metric-mem":
      return memTile(m.mem, "");
    case "metric-swap":
      return memTile(m.swap, "none");
    case "metric-temp":
      return m.cpu_temp_c != null && Number.isFinite(m.cpu_temp_c)
        ? {
            value: `${m.cpu_temp_c.toFixed(1)} \u00b0C`,
            detail:
              m.cpu_temp_c >= 80
                ? "throttling"
                : m.cpu_temp_c >= 70
                  ? "warm"
                  : "",
          }
        : { value: "—", detail: "" };
    case "metric-cpu":
      return derived.cpuPct != null
        ? { value: `${Math.round(derived.cpuPct)}%`, detail: "" }
        : { value: "—", detail: "" };
    case "metric-sd":
      return derived.sdReadBps != null
        ? {
           value: `${formatRate(derived.sdReadBps)} / ${formatRate(derived.sdWriteBps)}`,
           detail: "read / write",
          }
        : { value: "—", detail: "" };
    default:
      return { value: "—", detail: "" };
  }
}

export function MediaHub() {
  const [mappingDraft, setMappingDraft] = useState<MappingPrefs>(
    DEFAULT_MAPPING_PREFS,
  );
  const [mappingBaseline, setMappingBaseline] = useState<MappingPrefs>(
    DEFAULT_MAPPING_PREFS,
  );
  const [mappingSaving, setMappingSaving] = useState(false);
  const [mappingMsg, setMappingMsg] = useState<
    { kind: "info" | "success" | "error"; text: string } | null
  >(null);
  const [indexer, setIndexer] = useState<HealthBlock | null>(null);
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);
  const [storage, setStorage] = useState<StorageHealth | null>(null);
  const [fsckStatus, setFsckStatus] = useState<FsckStatusResponse | null>(null);
  const [fsckHistory, setFsckHistory] = useState<FsckHistoryEntry[]>([]);
  const [fsckLastChecks, setFsckLastChecks] = useState<Record<"part1" | "part2" | "part3", FsckLastCheckResponse | null>>({
    part1: null,
    part2: null,
    part3: null,
  });
  const [wifiStatus, setWifiStatus] = useState<WifiStatus | null>(null);
  const [wifiNetworks, setWifiNetworks] = useState<WifiNetwork[]>([]);
  const [wifiSaved, setWifiSaved] = useState<SavedWifiNetwork[]>([]);
  const [wifiOrder, setWifiOrder] = useState<string[]>([]);
  const [wifiJoinSel, setWifiJoinSel] = useState<string>("");
  const [wifiLoading, setWifiLoading] = useState(true);
  const [wifiScanLoading, setWifiScanLoading] = useState(false);
  // Which row has an inline form open, and what kind.
  const [wifiRowForm, setWifiRowForm] = useState<
    { ssid: string; mode: "join" | "forget" } | null
  >(null);
  const [wifiPsk, setWifiPsk] = useState("");
  // SSID currently being mutated (single-flight; disables all action buttons).
  const [wifiBusy, setWifiBusy] = useState<string | null>(null);
  // Inline status line (no toast system exists — surface feedback inline).
  const [wifiMsg, setWifiMsg] = useState<
    { kind: "info" | "success" | "error"; text: string } | null
  >(null);
  const [apStatus, setApStatus] = useState<ApInfo | null>(null);
  const [apLoading, setApLoading] = useState(true);
  const [apBusy, setApBusy] = useState(false);
  const [apMsg, setApMsg] = useState<
    { kind: "info" | "success" | "error"; text: string } | null
  >(null);
  const [apEditing, setApEditing] = useState(false);
  const [apSsid, setApSsid] = useState("");
  const [apPass, setApPass] = useState("");
  const [gadget, setGadget] = useState<GadgetModeStatus | null>(null);
  const [gadgetUnavailable, setGadgetUnavailable] = useState(false);
  const [derived, setDerived] = useState<{
    cpuPct: number | null;
    sdReadBps: number | null;
    sdWriteBps: number | null;
  }>({ cpuPct: null, sdReadBps: null, sdWriteBps: null });
  const prevSample = useRef<{
    total: number;
    idle: number;
    read: number;
    write: number;
    at: number;
  } | null>(null);
  const wifiScanCtrl = useRef<AbortController | null>(null);
  const wifiActionCtrl = useRef<AbortController | null>(null);
  const apActionCtrl = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    api
      .settings(ctrl.signal)
      .then((loadedPrefs) => {
        if (ctrl.signal.aborted) return;
        const next = mappingPrefsFromRows(loadedPrefs);
        setMappingDraft(next);
        setMappingBaseline(next);
      })
      .catch(() => {
        // Read-only degrade: fall back to template defaults without logging,
        // so an absent/empty prefs store never trips the zero-console gate.
      });
    // Device-status reads (5.1d). Each handler never 5xx and self-degrades to
    // unknown/null, so on the rare transport error we simply leave the section
    // in its loading/unknown state without logging (zero-console gate).
    api.systemHealth(ctrl.signal).then(setHealth).catch(() => {});
    api.storageHealth(ctrl.signal).then(setStorage).catch(() => {});
    api.fsckStatus(ctrl.signal).then(setFsckStatus).catch(() => {});
    api.fsckHistory(ctrl.signal).then(setFsckHistory).catch(() => {});
    Promise.all([
      api.fsckLastCheck(1, ctrl.signal),
      api.fsckLastCheck(2, ctrl.signal),
      api.fsckLastCheck(3, ctrl.signal),
    ])
      .then(([part1, part2, part3]) => {
        if (ctrl.signal.aborted) return;
        setFsckLastChecks({ part1, part2, part3 });
      })
      .catch(() => {});
    // USB-gadget mode status is the first cross-daemon control-socket read: it
    // talks to gadgetd's live socket and so, unlike the catalog reads, it CAN be
    // unavailable (gadgetd down / not running). Surface an honest "unavailable"
    // state on a real failure; ignore aborts (unmount) to keep the console clean.
    api
      .gadgetModeStatus(ctrl.signal)
      .then(setGadget)
      .catch(() => {
        if (!ctrl.signal.aborted) setGadgetUnavailable(true);
      });
    // Video Indexer status is the one System Health row that IS catalog data:
    // clip count + newest-clip age, derived from the read-only catalog. Every
    // other subsystem comes from /api/system/health (or stays unknown).
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

  const onSubmitMapping = async (event: Event) => {
    event.preventDefault();
    if (mappingSaving) return;
    const changed = (Object.keys(mappingDraft) as (keyof MappingPrefs)[])
      .filter((key) => mappingDraft[key] !== mappingBaseline[key])
      .map((key) => [key, mappingDraft[key]] as const);
    if (!changed.length) {
      setMappingMsg({ kind: "info", text: "No changes to save." });
      return;
    }
    setMappingSaving(true);
    setMappingMsg({ kind: "info", text: "Saving mapping settings…" });
    try {
      const ac = new AbortController();
      for (const [key, value] of changed) {
        await api.putSetting(key, value, ac.signal);
      }
      const nextBaseline = { ...mappingDraft };
      setMappingBaseline(nextBaseline);
      setMappingMsg({ kind: "success", text: "Mapping settings saved." });
    } catch (err) {
      setMappingMsg({
        kind: "error",
        text:
          err instanceof ApiError
            ? err.message
            : "Could not save mapping settings.",
      });
    } finally {
      setMappingSaving(false);
    }
  };

  useEffect(() => {
    let active = true;
    const ctrl = new AbortController();
    setWifiLoading(true);
    Promise.all([
      api.wifiStatus(ctrl.signal),
      api.wifiNetworks(ctrl.signal),
      api.wifiSaved(ctrl.signal),
    ])
      .then(([status, networks, saved]) => {
        if (!active) return;
        setWifiStatus(status);
        setWifiNetworks(networks.networks);
        setWifiSaved(saved.networks);
      })
      .catch((err) => {
        if (!active || isAbortError(err)) return;
        setWifiStatus(null);
        setWifiNetworks([]);
        setWifiSaved([]);
      })
      .finally(() => {
        if (active) setWifiLoading(false);
      });
    return () => {
      active = false;
      ctrl.abort();
    };
  }, []);

  useEffect(() => {
    let active = true;
    const ctrl = new AbortController();
    setApLoading(true);
    api
      .wifiApStatus(ctrl.signal)
      .then((resp) => {
        if (!active) return;
        setApStatus(resp.ap);
      })
      .catch((err) => {
        if (!active || isAbortError(err)) return;
        setApStatus(null);
      })
      .finally(() => {
        if (active) setApLoading(false);
      });
    return () => {
      active = false;
      ctrl.abort();
    };
  }, []);

  useEffect(
    () => () => {
      wifiScanCtrl.current?.abort();
      wifiActionCtrl.current?.abort();
      apActionCtrl.current?.abort();
    },
    [],
  );

  useEffect(() => {
    const nextOrder = wifiSaved.map((n) => n.ssid);
    setWifiOrder(nextOrder);
  }, [wifiSaved]);

  useEffect(() => {
    let active = true;
    let ctrl: AbortController | null = null;
    const tick = () => {
      ctrl?.abort();
      ctrl = new AbortController();
      api
        .systemMetrics(ctrl.signal)
        .then((m) => {
          if (!active) return;
          setMetrics(m);
          const now = Date.now();
          const prev = prevSample.current;
          let next = {
            cpuPct: null as number | null,
            sdReadBps: null as number | null,
            sdWriteBps: null as number | null,
          };
          if (
            m.cpu_times &&
            prev &&
            m.cpu_times.total > prev.total &&
            m.cpu_times.idle >= prev.idle
          ) {
            const dTotal = m.cpu_times.total - prev.total;
            const dIdle = m.cpu_times.idle - prev.idle;
            next.cpuPct = Math.max(0, Math.min(100, 100 * (1 - dIdle / dTotal)));
          }
          if (m.sd_io && prev) {
            const secs = (now - prev.at) / 1000;
            if (
              secs > 0 &&
              m.sd_io.read_bytes >= prev.read &&
              m.sd_io.write_bytes >= prev.write
            ) {
              next.sdReadBps = (m.sd_io.read_bytes - prev.read) / secs;
              next.sdWriteBps = (m.sd_io.write_bytes - prev.write) / secs;
            }
          }
          setDerived(next);
          // Advance each counter's baseline independently: a poll that is
          // missing one source must not freeze the other metric's delta.
          prevSample.current = {
            total: m.cpu_times?.total ?? prev?.total ?? 0,
            idle: m.cpu_times?.idle ?? prev?.idle ?? 0,
            read: m.sd_io?.read_bytes ?? prev?.read ?? 0,
            write: m.sd_io?.write_bytes ?? prev?.write ?? 0,
            at: now,
          };
        })
        .catch(() => {
          // Read-only degrade; ignore aborts/transport errors (zero-console gate).
        });
    };
    tick();
    const h = setInterval(tick, 2000);
    return () => {
      active = false;
      clearInterval(h);
      ctrl?.abort();
    };
  }, []);

  const onWifiScan = () => {
    if (wifiScanLoading) return;
    wifiScanCtrl.current?.abort();
    const ctrl = new AbortController();
    wifiScanCtrl.current = ctrl;
    setWifiScanLoading(true);
    api
      .wifiScan(ctrl.signal)
      .then((networks) => {
        setWifiNetworks(networks.networks);
        return api
          .wifiStatus(ctrl.signal)
          .then(setWifiStatus)
          .catch((err) => {
            if (!isAbortError(err)) setWifiStatus(null);
          });
      })
      .catch((err) => {
        if (!isAbortError(err)) setWifiNetworks([]);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setWifiScanLoading(false);
      });
  };

  const reloadWifi = (signal: AbortSignal) =>
    Promise.all([api.wifiStatus(signal), api.wifiNetworks(signal), api.wifiSaved(signal)]).then(
      ([status, networks, saved]) => {
        setWifiStatus(status);
        setWifiNetworks(networks.networks);
        setWifiSaved(saved.networks);
      },
    );

  const reloadAp = (signal: AbortSignal) =>
    api.wifiApStatus(signal).then((resp) => {
      setApStatus(resp.ap);
    });

  const runConnect = (ssid: string, psk?: string) => {
    if (wifiBusy) return;
    wifiActionCtrl.current?.abort();
    const ctrl = new AbortController();
    wifiActionCtrl.current = ctrl;
    setWifiBusy(ssid);
    setWifiMsg({ kind: "info", text: `Connecting to ${ssid}…` });
    api
      .wifiConnect({ ssid, ...(psk ? { psk } : {}) }, ctrl.signal)
      .then((resp) => {
        if (ctrl.signal.aborted) return;
        setWifiRowForm(null);
        setWifiPsk("");
        setWifiJoinSel("");
        setWifiMsg({
          kind: resp.connected ? "success" : "error",
          text: resp.connected
            ? `Connected to ${ssid}${resp.ip ? ` (${resp.ip})` : ""}`
            : `Could not connect to ${ssid}`,
        });
        return reloadWifi(ctrl.signal).catch(() => {});
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setWifiMsg({
          kind: "error",
          text: err instanceof ApiError ? err.message : `Could not connect to ${ssid}`,
        });
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setWifiBusy(null);
      });
  };

  const runForget = (ssid: string) => {
    if (wifiBusy) return;
    wifiActionCtrl.current?.abort();
    const ctrl = new AbortController();
    wifiActionCtrl.current = ctrl;
    setWifiBusy(ssid);
    setWifiMsg({ kind: "info", text: `Removing ${ssid}…` });
    api
      .wifiForget(ssid, ctrl.signal)
      .then((resp) => {
        if (ctrl.signal.aborted) return;
        setWifiRowForm(null);
        setWifiMsg({
          kind: resp.forgotten ? "success" : "error",
          text: resp.forgotten
            ? `Removed saved network ${ssid}`
            : `Could not remove ${ssid}`,
        });
        return reloadWifi(ctrl.signal).catch(() => {});
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setWifiMsg({
          kind: "error",
          text: err instanceof ApiError ? err.message : `Could not remove ${ssid}`,
        });
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setWifiBusy(null);
      });
  };

  const runSelect = (ssid: string) => {
    if (wifiBusy) return;
    wifiActionCtrl.current?.abort();
    const ctrl = new AbortController();
    wifiActionCtrl.current = ctrl;
    setWifiBusy(ssid);
    setWifiMsg({ kind: "info", text: `Connecting to ${ssid}…` });
    api
      .wifiSelect(ssid, ctrl.signal)
      .then((resp) => {
        if (ctrl.signal.aborted) return;
        setWifiMsg({
          kind: resp.connected ? "success" : "error",
          text: resp.connected
            ? `Connected to ${ssid}${resp.ip ? ` (${resp.ip})` : ""}`
            : `Could not connect to ${ssid}`,
        });
        return reloadWifi(ctrl.signal).catch(() => {});
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        const reverted =
          err instanceof ApiError && err.code === "wifi_select_timeout";
        setWifiMsg({
          kind: "error",
          text: reverted
            ? `Could not connect to ${ssid} — kept your previous network`
            : err instanceof ApiError
              ? err.message
              : `Could not connect to ${ssid}`,
        });
        return reloadWifi(ctrl.signal).catch(() => {});
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setWifiBusy(null);
      });
  };

  const applyApMode = (mode: ApMode) => {
    if (apBusy) return;
    apActionCtrl.current?.abort();
    const ctrl = new AbortController();
    apActionCtrl.current = ctrl;
    setApBusy(true);
    setApMsg({ kind: "info", text: "Updating access point…" });
    api
      .wifiApMode(mode, ctrl.signal)
      .then(() => {
        if (ctrl.signal.aborted) return;
        setApMsg({ kind: "success", text: "Access point updated" });
        return reloadAp(ctrl.signal).catch(() => {});
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setApMsg({
          kind: "error",
          text: err instanceof ApiError ? err.message : "Could not update access point",
        });
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setApBusy(false);
      });
  };

  const saveApConfig = () => {
    if (apBusy) return;
    const ssid = apSsid.trim();
    if (ssid.length === 0) {
      setApMsg({ kind: "error", text: "Access point name is required" });
      return;
    }
    if (apPass.length < 8 || apPass.length > 63) {
      setApMsg({
        kind: "error",
        text: "Password must be 8–63 characters",
      });
      return;
    }
    apActionCtrl.current?.abort();
    const ctrl = new AbortController();
    apActionCtrl.current = ctrl;
    setApBusy(true);
    setApMsg({ kind: "info", text: "Saving access point settings…" });
    api
      .wifiApConfig({ ssid, passphrase: apPass }, ctrl.signal)
      .then(() => {
        if (ctrl.signal.aborted) return;
        setApPass("");
        setApEditing(false);
        setApMsg({ kind: "success", text: "Access point settings saved" });
        return reloadAp(ctrl.signal).catch(() => {});
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setApMsg({
          kind: "error",
          text: err instanceof ApiError ? err.message : "Could not save access point settings",
        });
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setApBusy(false);
      });
  };

  const moveWifi = (idx: number, dir: -1 | 1) => {
    setWifiOrder((prev) => {
      const next = prev.slice();
      const j = idx + dir;
      if (j < 0 || j >= next.length) return prev;
      [next[idx], next[j]] = [next[j], next[idx]];
      return next;
    });
  };

  const saveWifiOrder = () => {
    if (wifiBusy) return;
    wifiActionCtrl.current?.abort();
    const ctrl = new AbortController();
    wifiActionCtrl.current = ctrl;
    setWifiBusy("__order__");
    setWifiMsg({ kind: "info", text: "Saving priority order…" });
    api
      .wifiPriority({ order: wifiOrder }, ctrl.signal)
      .then(() => {
        if (ctrl.signal.aborted) return;
        setWifiMsg({ kind: "success", text: "Priority order saved" });
        return reloadWifi(ctrl.signal).catch(() => {});
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setWifiMsg({ kind: "error", text: "Could not save priority order" });
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setWifiBusy(null);
      });
  };

  const startJoin = (network: WifiNetwork) => {
    if (wifiBusy) return;
    setWifiMsg(null);
    if (network.security) {
      // Secured + unsaved → reveal inline PSK form.
      setWifiPsk("");
      setWifiRowForm({ ssid: network.ssid, mode: "join" });
    } else {
      // Open network → connect immediately.
      void runConnect(network.ssid);
    }
  };

  const startForget = (ssid: string) => {
    if (wifiBusy) return;
    setWifiMsg(null);
    setWifiRowForm({ ssid, mode: "forget" });
  };

  const cancelRowForm = () => {
    setWifiRowForm(null);
    setWifiPsk("");
  };

  const overall = health?.overall ?? "unknown";
  const statusCopy = STATUS_COPY[overall] ?? STATUS_COPY.unknown;
  const wifiServerOrder = wifiSaved.map((n) => n.ssid);
  const wifiOrderDirty = wifiOrder.join("\u0001") !== wifiServerOrder.join("\u0001");
  const orderedSaved = wifiOrder
    .map((ssid) => wifiSaved.find((n) => n.ssid === ssid))
    .filter((n): n is SavedWifiNetwork => n != null);
  const availableWifi = wifiNetworks.filter((n) => !n.saved && !n.active);
  const apMode = apStatus?.mode ?? "auto";
  const fsckRecent = fsckHistory.slice(-5).reverse();
  const fsckStatusLine =
    fsckStatus == null
      ? "Filesystem check status unavailable."
      : fsckStatus.running
        ? `${fsckStatus.partition ? partitionLabel(fsckStatus.partition) : "Filesystem"} check running${fsckStatus.progress ? ` — ${fsckStatus.progress}` : ""}`
        : fsckStatus.result
          ? `Last result: ${fsckResultLabel(fsckStatus.result)}${fsckStatus.details ? ` — ${fsckStatus.details}` : ""}`
          : "No filesystem check currently running.";

  return (
    // Bare screen content — the router hoists a single shared <Shell> and
    // supplies the active nav key (this Path-A dashboard is routed at
    // `/settings` → "settings"), so this screen no longer wraps itself in Shell
    // (was <Shell active="settings"> in the standalone 5.2 build before the 5.3
    // router landed).
    <div class="container" data-screen="settings-dashboard">
        {/* Device Status — derived from /api/system/health overall severity;
            falls back to the baseline "unknown" copy until the probe resolves. */}
        <div class={`device-status-card device-status-${overall}`}>
          <div class="device-status-header">
            <span
              class={`status-dot status-${overall}`}
              style={`background:${SEV_COLORS[overall] ?? SEV_COLORS.unknown};`}
            />
            <div class="device-status-info">
              <strong>{statusCopy.title}</strong>
              <p>{statusCopy.detail}</p>
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
                  class={`health-dot health-dot-${overall}`}
                  aria-hidden="true"
                  style={`width:10px; height:10px; border-radius:50%; display:inline-block; flex-shrink:0; background:${SEV_COLORS[overall] ?? SEV_COLORS.unknown};`}
                />
                <span id="system-health-overall-text">
                  {OVERALL_LABEL[overall] ?? OVERALL_LABEL.unknown}
                </span>
              </p>
              <div
                id="system-health-rows"
                style="display:grid; grid-template-columns:auto auto 1fr; gap:6px 12px; align-items:start; font-size:0.9rem;"
              >
                {SUBSYSTEMS.map((sub) => {
                  let sev: string;
                  let msg: string;
                  if (sub.key === "indexer") {
                    const webdIdx = health?.subsystems?.indexer ?? null;
                    const cat = indexer;
                    sev =
                      webdIdx && webdIdx.severity !== "unknown"
                        ? webdIdx.severity
                        : (cat?.severity ?? "unknown");
                    const catMsg = cat?.message ?? null;
                    if (webdIdx && (webdIdx.severity === "warn" || webdIdx.severity === "error")) {
                      msg = catMsg ? `${webdIdx.message} — ${catMsg}` : webdIdx.message;
                    } else {
                      msg = catMsg ?? "—";
                    }
                  } else {
                    const block = health?.subsystems?.[sub.key] ?? null;
                    sev = block?.severity ?? "unknown";
                    const details = (sub.key === "journal" ? block?.details : undefined) ?? [];
                    const detailText = details.length > 0 ? details.join(" | ") : null;
                    msg = detailText ? `${block?.message ?? "—"} — ${detailText}` : (block?.message ?? "—");
                  }
                  return (
                    <Fragment key={sub.key}>
                      <div>
                        <span
                          aria-label={sev}
                          style={`width:10px; height:10px; border-radius:50%; display:inline-block; flex-shrink:0; background:${SEV_COLORS[sev] ?? SEV_COLORS.unknown};`}
                        />
                      </div>
                      <div style="color:var(--text-primary)">{sub.label}</div>
                      <div style="color:var(--text-secondary); min-width:0; overflow-wrap:anywhere;">
                        {msg}
                      </div>
                    </Fragment>
                  );
                })}
              </div>
            </div>
          </div>
        </details>

        {/* Live Metrics — load/temp/memory come from probe payload; CPU and SD
            throughput are sampled client-side as deltas across polls. */}
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
                {METRIC_TILES.map((t) => {
                  const { value, detail } = metricFor(t.id, metrics, derived);
                  return (
                    <div class="metric-tile" id={t.id} key={t.id}>
                      <div class="metric-label">{t.label}</div>
                      <div class="metric-value">{value}</div>
                      <div class="metric-detail">{detail || "\u00a0"}</div>
                    </div>
                  );
                })}
              </div>
              <p
                id="live-metrics-foot"
                style="margin:0; font-size:0.78rem; color:var(--text-secondary);"
              >
                Updated{" "}
                <span id="live-metrics-updated">
                  {formatUpdated(metrics?.updated_at)}
                </span>{" "}
                ·{" "}
                <span id="live-metrics-uptime">
                  {formatUptime(metrics?.uptime_s)}
                </span>
              </p>
            </div>
          </div>
        </details>

        {/* USB Drive — live state from gadgetd's control socket
            (GET /api/gadget/mode-status), the first cross-daemon control-socket read
            surfaced in the SPA. Unlike the catalog reads this CAN be unavailable
            (gadgetd down / not running), in which case we show an honest
            "unavailable" state rather than fabricating a "connected" status. */}
        <details class="settings-section" id="usb-gadget-section" open>
          <summary>USB Drive</summary>
          <div class="section-content">
            {gadget ? (
              <div
                id="usb-gadget-card"
                style="display:grid; grid-template-columns:auto 1fr; gap:6px 12px; font-size:0.9rem; align-items:start;"
              >
                <div style="color:var(--text-secondary)">Presented to car</div>
                <div data-testid="usb-present" style="color:var(--text-primary)">
                  {gadget.present ? "Yes" : "No"}
                </div>
                <div style="color:var(--text-secondary)">Gadget mode</div>
                <div data-testid="usb-mode" style="color:var(--text-primary)">
                  {gadgetModeLabel(gadget.mode)}
                </div>
                <div style="color:var(--text-secondary)">Controller bound</div>
                <div data-testid="usb-bound" style="color:var(--text-primary)">
                  {gadget.bound
                    ? `Yes (${gadget.udc_state ?? "unknown"})`
                    : "No"}
                </div>
                <div style="color:var(--text-secondary)">Handoff state</div>
                <div data-testid="usb-handoff" style="color:var(--text-primary)">
                  {gadgetHandoffLabel(gadget)}
                </div>
                <div style="color:var(--text-secondary)">TeslaCam LUN image</div>
                <div style="color:var(--text-primary); min-width:0; overflow-wrap:anywhere;">
                  {lunStatusLabel(gadget.lun.teslacam.image, gadget.lun.teslacam.loaded)}
                </div>
                <div style="color:var(--text-secondary)">Media LUN image</div>
                <div style="color:var(--text-primary); min-width:0; overflow-wrap:anywhere;">
                  {lunStatusLabel(gadget.lun.media.image, gadget.lun.media.loaded)}
                </div>
                <div style="color:var(--text-secondary)">Recovery / banner</div>
                <div
                  data-testid="usb-recovery"
                  style="color:var(--text-primary); min-width:0; overflow-wrap:anywhere;"
                >
                  {recoveryLabel(gadget)}
                </div>
                <div style="color:var(--text-secondary)">Media mount (read)</div>
                <div
                  data-testid="usb-media-ro"
                  style="color:var(--text-primary); min-width:0; overflow-wrap:anywhere;"
                >
                  {gadget.recovery.media_ro_mounted === null
                    ? "\u2014"
                    : gadget.recovery.media_ro_mounted
                      ? `Mounted${gadget.recovery.media_ro_path ? ` (${gadget.recovery.media_ro_path})` : ""}`
                      : `Not mounted${gadget.recovery.media_ro_error ? ` \u2014 ${gadget.recovery.media_ro_error}` : ""}`}
                </div>
              </div>
            ) : gadgetUnavailable ? (
              <div
                data-testid="usb-gadget-unavailable"
                style="text-align:center; padding:12px; color:var(--text-secondary)"
              >
                USB gadget status is unavailable (gadgetd is not reachable).
              </div>
            ) : (
              <div
                data-testid="usb-gadget-loading"
                style="text-align:center; padding:12px; color:var(--text-secondary)"
              >
                Loading USB status&#8230;
              </div>
            )}
          </div>
        </details>

        {/* WiFi Networks — NetworkManager view with join/forget controls. */}
        <details class="settings-section" id="wifi-networks-section">
          <summary>WiFi Networks</summary>
          <div class="section-content">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
              <p style="font-size:0.85em;color:var(--text-secondary);margin:0">
                Configured networks are listed in priority order — the device
                prefers the highest-priority network in range.
              </p>
              <button
                type="button"
                class="edit-btn"
                id="btnWifiScan"
                style="padding:4px 10px;font-size:0.85em;white-space:nowrap"
                disabled={wifiLoading || wifiScanLoading || wifiBusy != null}
                onClick={onWifiScan}
              >
                {wifiScanLoading ? "Scanning…" : "Scan"}
              </button>
            </div>
            <div id="savedNetworksList">
              {wifiMsg ? (
                <div
                  id="wifi-action-status"
                  role="status"
                  aria-live="polite"
                  style={`font-size:0.85em;margin:2px 0 6px;color:${
                    wifiMsg.kind === "error"
                      ? "var(--accent-error, #e53935)"
                      : wifiMsg.kind === "success"
                        ? "var(--accent-success, #4caf50)"
                        : "var(--text-secondary)"
                  }`}
                >
                  {wifiMsg.text}
                </div>
              ) : null}
              {wifiLoading ? (
                <div style="text-align:center;padding:12px;color:var(--text-secondary)">
                  Loading Wi-Fi networks…
                </div>
              ) : (
                <div style="display:flex;flex-direction:column;gap:8px">
                  <div
                    id="wifi-current-connection"
                    style="display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border:1px solid var(--border-color);border-radius:8px;background:var(--bg-secondary)"
                  >
                    <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                      <strong>{wifiStatus?.ssid ?? "Not connected"}</strong>
                      {wifiStatus?.signal != null ? (
                        <span style="margin-left:8px;color:var(--text-secondary);font-size:0.85em">
                          {wifiSignalBars(wifiStatus.signal)} {wifiStatus.signal}%
                        </span>
                      ) : null}
                    </div>
                    {wifiStatus?.connected ? (
                      <span
                        style="font-size:0.75em;padding:2px 8px;border-radius:999px;background:var(--bg-secondary);border:1px solid var(--accent-success, #4caf50);color:var(--accent-success, #4caf50)"
                      >
                        Connected
                      </span>
                    ) : null}
                  </div>
                  <div id="wifi-saved-list" style="display:flex;flex-direction:column;gap:6px">
                    <h4 style="margin:0;font-size:0.95em">Configured networks</h4>
                    {wifiSaved.length > 0 ? (
                      <Fragment>
                        {orderedSaved.map((sn, idx) => {
                          const scan = wifiNetworks.find((n) => n.ssid === sn.ssid);
                          const isActive =
                            sn.active ||
                            (!!wifiStatus?.connected && wifiStatus?.ssid === sn.ssid);
                          return (
                            <div
                              key={sn.ssid}
                              class="wifi-network-row wifi-saved-row"
                              data-ssid={sn.ssid}
                              style="display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:8px 10px;border:1px solid var(--border-color);border-radius:8px"
                            >
                              <div style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                                {sn.ssid}
                              </div>
                              <div style="font-size:0.85em;color:var(--text-secondary);white-space:nowrap">
                                {scan ? (
                                  <Fragment>
                                    {wifiSignalBars(scan.signal)} {scan.signal}%
                                  </Fragment>
                                ) : (
                                  "Not in range"
                                )}
                              </div>
                              <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px">
                                {scan?.security ? <span aria-label="Secured">🔒</span> : null}
                                {isActive ? (
                                  <span
                                    style="font-size:0.75em;padding:2px 8px;border-radius:999px;background:var(--bg-secondary);border:1px solid var(--accent-success, #4caf50);color:var(--accent-success, #4caf50)"
                                  >
                                    Connected
                                  </span>
                                ) : null}
                                {!isActive ? (
                                  <button
                                    type="button"
                                    class="edit-btn wifi-connect-btn"
                                    data-ssid={sn.ssid}
                                    disabled={wifiBusy != null}
                                    onClick={() => runSelect(sn.ssid)}
                                    style="padding:4px 10px;font-size:0.85em;white-space:nowrap"
                                  >
                                    Connect
                                  </button>
                                ) : null}
                                {!isActive ? (
                                  <button
                                    type="button"
                                    class="edit-btn wifi-forget-btn"
                                    data-ssid={sn.ssid}
                                    disabled={wifiBusy != null}
                                    onClick={() => startForget(sn.ssid)}
                                    style="padding:4px 10px;font-size:0.85em;white-space:nowrap"
                                  >
                                    Forget
                                  </button>
                                ) : null}
                                <button
                                  type="button"
                                  class="edit-btn wifi-move-up"
                                  aria-label="Move up"
                                  disabled={wifiBusy != null || idx === 0}
                                  onClick={() => moveWifi(idx, -1)}
                                  style="padding:4px 10px;font-size:0.85em;white-space:nowrap"
                                >
                                  ↑
                                </button>
                                <button
                                  type="button"
                                  class="edit-btn wifi-move-down"
                                  aria-label="Move down"
                                  disabled={wifiBusy != null || idx === orderedSaved.length - 1}
                                  onClick={() => moveWifi(idx, 1)}
                                  style="padding:4px 10px;font-size:0.85em;white-space:nowrap"
                                >
                                  ↓
                                </button>
                              </div>
                              {wifiRowForm?.ssid === sn.ssid && wifiRowForm.mode === "forget" ? (
                                <div style="display:flex;gap:6px;align-items:center;margin-top:6px;width:100%">
                                  <span style="flex:1;min-width:0;font-size:0.85em;color:var(--text-secondary)">
                                    Forget this saved network?
                                  </span>
                                  <button
                                    type="button"
                                    class="edit-btn wifi-forget-confirm"
                                    disabled={wifiBusy != null}
                                    onClick={() => runForget(sn.ssid)}
                                    style="padding:4px 10px;font-size:0.85em"
                                  >
                                    Confirm
                                  </button>
                                  <button
                                    type="button"
                                    class="edit-btn wifi-forget-cancel"
                                    disabled={wifiBusy != null}
                                    onClick={cancelRowForm}
                                    style="padding:4px 10px;font-size:0.85em"
                                  >
                                    Cancel
                                  </button>
                                </div>
                              ) : null}
                            </div>
                          );
                        })}
                        {orderedSaved.length > 1 ? (
                          <div style="display:flex;flex-direction:column;gap:6px;padding-top:2px">
                            <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
                              <button
                                type="button"
                                class="edit-btn"
                                id="wifi-order-save"
                                disabled={wifiBusy != null || !wifiOrderDirty}
                                onClick={saveWifiOrder}
                                style="padding:4px 10px;font-size:0.85em"
                              >
                                Save priority order
                              </button>
                              <button
                                type="button"
                                class="edit-btn"
                                id="wifi-order-reset"
                                disabled={wifiBusy != null || !wifiOrderDirty}
                                onClick={() => setWifiOrder(wifiServerOrder)}
                                style="padding:4px 10px;font-size:0.85em"
                              >
                                Reset
                              </button>
                            </div>
                            <span style="font-size:0.85em;color:var(--text-secondary)">
                              Higher = preferred when in range.
                            </span>
                          </div>
                        ) : null}
                      </Fragment>
                    ) : (
                      <div style="color:var(--text-secondary);font-size:0.9em">
                        No configured networks.
                      </div>
                    )}
                  </div>

                  <div style="display:flex;flex-direction:column;gap:6px">
                    <h4 style="margin:0;font-size:0.95em">Available networks</h4>
                    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                      <select
                        id="wifi-available-select"
                        value={wifiJoinSel}
                        disabled={wifiBusy != null || availableWifi.length === 0}
                        onChange={(e) =>
                          setWifiJoinSel((e.target as HTMLSelectElement).value)
                        }
                        style="flex:1;min-width:220px;padding:4px 8px;font-size:0.9em"
                      >
                        <option value="">
                          {availableWifi.length
                            ? "Select a network to join…"
                            : "No new networks in range"}
                        </option>
                        {availableWifi.map((n) => (
                          <option key={n.ssid} value={n.ssid}>
                            {n.ssid} · {n.signal}%
                            {n.security ? " 🔒" : ""}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        class="edit-btn"
                        id="wifi-join-btn"
                        disabled={wifiBusy != null || wifiJoinSel === ""}
                        onClick={() => {
                          const net = availableWifi.find((n) => n.ssid === wifiJoinSel);
                          if (net) startJoin(net);
                        }}
                        style="padding:4px 10px;font-size:0.85em;white-space:nowrap"
                      >
                        Join
                      </button>
                    </div>
                    {wifiRowForm?.mode === "join" ? (
                      <div style="display:flex;flex-direction:column;gap:6px;max-width:480px">
                        <label
                          for="wifi-join-password"
                          style="font-size:0.85em;color:var(--text-secondary)"
                        >
                          Password for {wifiRowForm.ssid}
                        </label>
                        <div style="display:flex;gap:6px;align-items:center">
                          <input
                            id="wifi-join-password"
                            type="password"
                            class="wifi-psk-input"
                            placeholder="Wi-Fi password"
                            value={wifiPsk}
                            disabled={wifiBusy != null}
                            onInput={(e) =>
                              setWifiPsk((e.target as HTMLInputElement).value)
                            }
                            style="flex:1;min-width:0;padding:4px 8px;font-size:0.85em"
                          />
                          <button
                            type="button"
                            class="edit-btn wifi-connect-confirm"
                            disabled={wifiBusy != null || wifiPsk.length === 0}
                            onClick={() => runConnect(wifiRowForm.ssid, wifiPsk)}
                            style="padding:4px 10px;font-size:0.85em"
                          >
                            Connect
                          </button>
                          <button
                            type="button"
                            class="edit-btn wifi-join-cancel"
                            disabled={wifiBusy != null}
                            onClick={cancelRowForm}
                            style="padding:4px 10px;font-size:0.85em"
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    ) : null}
                  </div>
                </div>
              )}
            </div>
          </div>
        </details>

        <details class="settings-section" id="access-point-section">
          <summary>Access Point</summary>
          <div class="section-content">
            {apMsg ? (
              <div
                id="ap-action-status"
                role="status"
                aria-live="polite"
                style={`font-size:0.85em;margin:2px 0 6px;color:${
                  apMsg.kind === "error"
                    ? "var(--accent-error, #e53935)"
                    : apMsg.kind === "success"
                      ? "var(--accent-success, #4caf50)"
                      : "var(--text-secondary)"
                }`}
              >
                {apMsg.text}
              </div>
            ) : null}
            {apLoading ? (
              <div style="text-align:center;padding:12px;color:var(--text-secondary)">
                Loading access point…
              </div>
            ) : (
              <div style="display:flex;flex-direction:column;gap:8px">
                <div
                  id="ap-status"
                  style="display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border:1px solid var(--border-color);border-radius:8px;background:var(--bg-secondary);gap:8px;flex-wrap:wrap"
                >
                  <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0">
                    <strong>{apStatus?.ssid ?? "(not configured)"}</strong>
                  </div>
                  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                    {apStatus?.active ? (
                      <span
                        style="font-size:0.75em;padding:2px 8px;border-radius:999px;background:var(--bg-secondary);border:1px solid var(--accent-success, #4caf50);color:var(--accent-success, #4caf50)"
                      >
                        Active
                      </span>
                    ) : (
                      <span
                        style="font-size:0.75em;padding:2px 8px;border-radius:999px;background:var(--bg-secondary);border:1px solid var(--border-color);color:var(--text-secondary)"
                      >
                        Inactive
                      </span>
                    )}
                    {apStatus?.active ? (
                      <span style="font-size:0.85em;color:var(--text-secondary)">
                        {apStatus.client_count} connected
                        {apStatus.ip ? ` · ${apStatus.ip}` : ""}
                      </span>
                    ) : null}
                  </div>
                </div>

                <div style="display:flex;flex-direction:column;gap:6px;max-width:520px">
                  <label for="ap-mode-select" style="font-size:0.85em;color:var(--text-secondary)">
                    Access point mode
                  </label>
                  <select
                    id="ap-mode-select"
                    value={apMode}
                    disabled={apBusy || apLoading}
                    onChange={(e) =>
                      applyApMode((e.target as HTMLSelectElement).value as ApMode)
                    }
                    style="padding:4px 8px;font-size:0.9em"
                  >
                    <option value="auto">Auto (recommended)</option>
                    <option value="force_on">On</option>
                    <option value="force_off">Off</option>
                  </select>
                  <p style="margin:0;font-size:0.85em;color:var(--text-secondary)">
                    {apMode === "force_on"
                      ? "The access point always broadcasts, even while Wi-Fi is connected."
                      : apMode === "force_off"
                        ? "The access point is disabled and never broadcasts."
                        : "The access point turns on only when the device cannot reach home Wi-Fi."}
                  </p>
                  {apMode === "force_off" ? (
                    <div
                      id="ap-lockout-warning"
                      style="font-size:0.85em;color:var(--error-text);border:1px solid var(--accent-error, #e53935);border-radius:8px;padding:8px 10px;background:var(--bg-secondary)"
                    >
                      With the access point off, if the device can't reach your Wi-Fi
                      you may lose access to it.
                    </div>
                  ) : null}
                </div>

                <div style="display:flex;flex-direction:column;gap:6px;max-width:520px">
                  <div>
                    <button
                      type="button"
                      class="edit-btn"
                      id="ap-edit-btn"
                      disabled={apBusy || apLoading}
                      onClick={() => {
                        if (!apEditing) {
                          setApSsid(apStatus?.ssid ?? "");
                          setApPass("");
                        }
                        setApEditing((v) => !v);
                      }}
                      style="padding:4px 10px;font-size:0.85em"
                    >
                      Edit AP name &amp; password
                    </button>
                  </div>
                  {apEditing ? (
                    <div style="display:flex;flex-direction:column;gap:6px;max-width:480px">
                      <label
                        for="ap-ssid-input"
                        style="font-size:0.85em;color:var(--text-secondary)"
                      >
                        Access point name
                      </label>
                      <input
                        id="ap-ssid-input"
                        value={apSsid}
                        disabled={apBusy}
                        onInput={(e) =>
                          setApSsid((e.target as HTMLInputElement).value)
                        }
                        style="padding:4px 8px;font-size:0.85em"
                      />
                      <label
                        for="ap-pass-input"
                        style="font-size:0.85em;color:var(--text-secondary)"
                      >
                        Password
                      </label>
                      <div style="display:flex;gap:6px;align-items:center">
                        <input
                          id="ap-pass-input"
                          type="password"
                          class="ap-pass-input"
                          placeholder="8–63 characters"
                          value={apPass}
                          disabled={apBusy}
                          onInput={(e) =>
                            setApPass((e.target as HTMLInputElement).value)
                          }
                          style="flex:1;min-width:0;padding:4px 8px;font-size:0.85em"
                        />
                        <button
                          type="button"
                          class="edit-btn ap-config-save"
                          disabled={apBusy}
                          onClick={saveApConfig}
                          style="padding:4px 10px;font-size:0.85em"
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          class="edit-btn"
                          disabled={apBusy}
                          onClick={() => {
                            setApEditing(false);
                            setApSsid(apStatus?.ssid ?? "");
                            setApPass("");
                          }}
                          style="padding:4px 10px;font-size:0.85em"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : null}
                </div>
              </div>
            )}
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

        {/* Mapping & Indexing — live config form bound to /api/settings. */}
        <details class="settings-section">
          <summary>Mapping &amp; Indexing</summary>
          <div class="section-content">
            <form onSubmit={onSubmitMapping}>
              <p style="font-size:0.85rem; color:var(--text-secondary); margin:0 0 16px">
                Tesla embeds GPS coordinates and telemetry data (speed, braking,
                steering) inside each dashcam video. The indexer extracts this
                data and builds a database of trips, routes, and driving events.
              </p>
              {mappingMsg ? (
                <div
                  id="mapping-action-status"
                  role="status"
                  aria-live="polite"
                  style={`font-size:0.85em;margin:0 0 10px;color:${
                    mappingMsg.kind === "error"
                      ? "var(--accent-error, #e53935)"
                      : mappingMsg.kind === "success"
                        ? "var(--accent-success, #4caf50)"
                        : "var(--text-secondary)"
                  }`}
                >
                  {mappingMsg.text}
                </div>
              ) : null}
              <div class="settings-form-grid">
                <div class="form-group">
                  <label style="font-size:0.85rem">
                    <strong>Trip gap (minutes)</strong>
                  </label>
                  <input
                    type="number"
                    name="trip_gap_minutes"
                    value={mappingDraft.trip_gap_minutes}
                    min="1"
                    max="60"
                    class="settings-form-input"
                    onInput={(e) => {
                      setMappingMsg(null);
                      setMappingDraft((prev) => ({
                        ...prev,
                        trip_gap_minutes: (e.currentTarget as HTMLInputElement).value,
                      }));
                    }}
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
                    value={mappingDraft.speed_limit_mph}
                    min="0"
                    max="200"
                    step="5"
                    class="settings-form-input"
                    onInput={(e) => {
                      setMappingMsg(null);
                      setMappingDraft((prev) => ({
                        ...prev,
                        speed_limit_mph: (e.currentTarget as HTMLInputElement).value,
                      }));
                    }}
                  />
                </div>
                <div class="form-group">
                  <label for="mapping-speed-units" style="font-size:0.85rem">
                    <strong>Map speed display units</strong>
                  </label>
                  <select
                    id="mapping-speed-units"
                    name="speed_unit"
                    class="settings-form-input"
                    value={mappingDraft.speed_unit}
                    onChange={(e) => {
                      setMappingMsg(null);
                      setMappingDraft((prev) => ({
                        ...prev,
                        speed_unit: (e.currentTarget as HTMLSelectElement).value,
                      }));
                    }}
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
                    value={mappingDraft.display_timezone}
                    onChange={(e) => {
                      setMappingMsg(null);
                      setMappingDraft((prev) => ({
                        ...prev,
                        display_timezone: (e.currentTarget as HTMLSelectElement).value,
                      }));
                    }}
                  >
                    <option value="">Auto (use this device's timezone)</option>
                    {TIMEZONES.map((tz) => (
                      <option value={tz} key={tz}>
                        {tz}{timezoneOffsetLabel(tz)}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <button
                type="submit"
                class="btn btn-primary"
                style="width:100%"
                disabled={mappingSaving}
              >
                {mappingSaving ? "Saving Mapping Settings…" : "Save Mapping Settings"}
              </button>
            </form>
          </div>
        </details>

        {/* Storage Health — from /api/storage/health. Capacity-derived severity
            + summary with best-effort wear telemetry probes. */}
        <details class="settings-section" id="storage-health-section">
          <summary>Storage Health</summary>
          <div class="section-content" id="storage-health-card">
            <div class="storage-health-header">
              <span
                class={`status-dot health-dot health-dot-${storage?.severity ?? "unknown"}`}
                id="storage-health-dot"
                aria-label="Storage health severity"
                style={`background:${SEV_COLORS[storage?.severity ?? "unknown"] ?? SEV_COLORS.unknown};`}
              />
              <strong id="storage-health-summary">
                {storage?.summary ?? "Checking…"}
              </strong>
            </div>
            <dl class="storage-health-grid" id="storage-health-grid">
              <dt>Device</dt>
              <dd>{storage?.device ?? "—"}</dd>
              <dt>Filesystem</dt>
              <dd>{storage?.fstype ?? "—"}</dd>
              <dt>Mount</dt>
              <dd>{storage?.mount ?? "—"}</dd>
              <dt>Filesystem errors</dt>
              <dd>{storage?.fs_errors == null ? "—" : String(storage.fs_errors)}</dd>
              <dt>TRIM</dt>
              <dd>{storage?.trim ?? "—"}</dd>
            </dl>
            <p class="storage-health-footer">
              SD cards expose no wear telemetry (no SMART, no per-block
              checksums). Plan to replace the card every 12 months and keep cloud
              archive enabled so a card failure never costs you data.
            </p>
          </div>
        </details>

        <details class="settings-section" id="fsck-section">
          <summary>Filesystem Health Check</summary>
          <div class="section-content">
            <p style="font-size:0.85rem; color:var(--text-secondary); margin:0 0 12px">
              Read-only visibility into persisted filesystem check state and
              recent results. Starting, cancelling, and repair actions are
              intentionally disabled in B-1.
            </p>
            <p
              id="fsck-status-line"
              data-testid="fsck-status-line"
              style="margin:0 0 10px; font-size:0.9rem"
            >
              {fsckStatusLine}
            </p>

            <div
              id="fsck-last-checks"
              style="display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); margin-bottom:12px"
            >
              {(["part1", "part2", "part3"] as const).map((partition) => {
                const last = fsckLastChecks[partition];
                const hasCheck = Boolean(last?.timestamp);
                return (
                  <article
                    key={partition}
                    data-testid={`fsck-last-${partition}`}
                    style="border:1px solid var(--border-color); border-radius:8px; padding:10px"
                  >
                    <strong>{partitionLabel(partition)}</strong>
                    <p style="margin:6px 0 0; color:var(--text-secondary); font-size:0.85rem">
                      {hasCheck
                        ? `Last checked ${formatLastCheckAge(last?.age_hours)}`
                        : "Never checked"}
                    </p>
                    {hasCheck ? (
                      <p style="margin:4px 0 0; font-size:0.85rem">
                        {fsckResultLabel(last?.result ?? null)}
                        {last?.details ? ` — ${last.details}` : ""}
                      </p>
                    ) : null}
                  </article>
                );
              })}
            </div>

            <div id="fsck-history">
              <strong style="font-size:0.9rem">Recent Checks</strong>
              {fsckRecent.length === 0 ? (
                <p
                  data-testid="fsck-history-empty"
                  style="margin:8px 0 0; color:var(--text-secondary); font-size:0.85rem"
                >
                  No filesystem checks recorded yet.
                </p>
              ) : (
                <table
                  class="fsck-history-table"
                  data-testid="fsck-history-table"
                  style="width:100%; margin-top:8px; border-collapse:collapse; font-size:0.85rem"
                >
                  <thead>
                    <tr style="text-align:left; border-bottom:1px solid var(--border-color)">
                      <th style="padding:6px">Time</th>
                      <th style="padding:6px">Drive</th>
                      <th style="padding:6px">Mode</th>
                      <th style="padding:6px">Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {fsckRecent.map((entry, idx) => (
                      <tr key={`${entry.timestamp}-${idx}`}>
                        <td style="padding:6px">
                          {new Date(entry.timestamp).toLocaleString()}
                        </td>
                        <td style="padding:6px">{partitionLabel(entry.partition)}</td>
                        <td style="padding:6px">{entry.mode}</td>
                        <td style="padding:6px">{fsckResultLabel(entry.result)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </details>

        {/* System — host facts read live from webd's /api/system/metrics;
            anything webd cannot observe renders as "—" rather than fabricated. */}
        <details class="settings-section">
          <summary>System</summary>
          <div class="section-content">
            <div style="display:grid; grid-template-columns:auto 1fr; gap:6px 16px; font-size:0.9rem;">
              <span style="color:var(--text-secondary)">Hostname</span>
              <strong>{metrics?.hostname ?? "—"}</strong>
              <span style="color:var(--text-secondary)">IP Address</span>
              <span>{metrics?.ip_address ?? "—"}</span>
              <span style="color:var(--text-secondary)">Uptime</span>
              <span>{formatUptime(metrics?.uptime_s)}</span>
              <span style="color:var(--text-secondary)">Platform</span>
              <span>{metrics?.platform ?? "—"}</span>
              <span style="color:var(--text-secondary)">Memory</span>
              <span>
                {metrics?.mem
                  ? `${humanBytes(metrics.mem.total_bytes - metrics.mem.available_bytes)} / ${humanBytes(metrics.mem.total_bytes)}`
                  : "—"}
              </span>
              <span style="color:var(--text-secondary)">Version</span>
              <code style="font-size:0.8rem">B-1</code>
            </div>
          </div>
        </details>
      </div>
  );
}
