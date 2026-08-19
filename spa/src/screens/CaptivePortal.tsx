import { Icon } from "../components/Icon";
import { useScreenHook } from "../components/screenHook";
import { ApiError, api } from "../api/client";
import type {
  ApMode,
  ApInfo,
  SavedWifiResponse,
  SavedWifiNetwork,
  WifiNetworksResponse,
  WifiStatus,
} from "../api/types";
import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import "../styles/captive-portal.css";

/**
 * Captive Portal / Wi-Fi setup screen (route `/captive-portal`), using only
 * existing typed webd APIs under `/api/wifi*` and `/api/wifi/ap*`.
 * Mutations stay explicitly operator-confirmed in the UI.
 */

type ActionNoticeKind = "info" | "success" | "error";
type ActionNotice = { kind: ActionNoticeKind; text: string } | null;
type SafeApMode = "auto" | "force_on";

function isAbortError(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    "name" in err &&
    (err as { name?: string }).name === "AbortError"
  );
}

function actionErrorText(err: unknown, fallback: string): string {
  if (err instanceof ApiError && err.message) return err.message;
  return fallback;
}

function validPsk(psk: string): boolean {
  if (psk.length === 0) return true;
  return (
    (psk.length >= 8 && psk.length <= 63) ||
    (psk.length === 64 && /^[0-9a-fA-F]+$/.test(psk))
  );
}

function confirmOperatorAction(message: string): boolean {
  if (typeof window === "undefined") return true;
  return window.confirm(message);
}

function toSafeApMode(mode: ApMode): SafeApMode {
  return mode === "force_on" ? "force_on" : "auto";
}

export function CaptivePortal() {
  useScreenHook("captive-portal");
  const [status, setStatus] = useState<WifiStatus | null>(null);
  const [networks, setNetworks] = useState<WifiNetworksResponse | null>(null);
  const [saved, setSaved] = useState<SavedWifiResponse | null>(null);
  const [apStatus, setApStatus] = useState<ApInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(true);
  const [snapshotRefreshing, setSnapshotRefreshing] = useState(false);

  const [notice, setNotice] = useState<ActionNotice>(null);
  const [scanBusy, setScanBusy] = useState(false);
  const [wifiBusy, setWifiBusy] = useState<string | null>(null);
  const [priorityBusy, setPriorityBusy] = useState(false);
  const [apBusy, setApBusy] = useState(false);

  const [manualSsid, setManualSsid] = useState("");
  const [manualPsk, setManualPsk] = useState("");
  const [priorityOrder, setPriorityOrder] = useState<string[]>([]);

  const [apModeDraft, setApModeDraft] = useState<SafeApMode>("auto");
  const [apEditing, setApEditing] = useState(false);
  const [apSsidDraft, setApSsidDraft] = useState("");
  const [apPassphraseDraft, setApPassphraseDraft] = useState("");

  const loadCtrl = useRef<AbortController | null>(null);
  const wifiActionCtrl = useRef<AbortController | null>(null);
  const apActionCtrl = useRef<AbortController | null>(null);
  const apEditingRef = useRef(false);

  useEffect(() => {
    apEditingRef.current = apEditing;
  }, [apEditing]);

  const reloadSnapshot = useCallback((isRefresh: boolean) => {
    loadCtrl.current?.abort();
    const ctrl = new AbortController();
    loadCtrl.current = ctrl;
    if (isRefresh) setSnapshotRefreshing(true);
    else setSnapshotLoading(true);
    setLoadError(null);
    Promise.allSettled([
      api.wifiStatus(ctrl.signal),
      api.wifiNetworks(ctrl.signal),
      api.wifiSaved(ctrl.signal),
      api.wifiApStatus(ctrl.signal),
    ])
      .then(([statusResult, networksResult, savedResult, apResult]) => {
        if (ctrl.signal.aborted) return;
        const errors: string[] = [];
        if (statusResult.status === "fulfilled") setStatus(statusResult.value);
        else errors.push(actionErrorText(statusResult.reason, "Wi-Fi status unavailable."));
        if (networksResult.status === "fulfilled") setNetworks(networksResult.value);
        else errors.push(actionErrorText(networksResult.reason, "Wi-Fi scan data unavailable."));
        if (savedResult.status === "fulfilled") setSaved(savedResult.value);
        else errors.push(actionErrorText(savedResult.reason, "Saved Wi-Fi profiles unavailable."));
        if (apResult.status === "fulfilled") {
          setApStatus(apResult.value.ap);
          setApModeDraft(toSafeApMode(apResult.value.ap.mode));
          if (!apEditingRef.current) setApSsidDraft(apResult.value.ap.ssid ?? "");
        } else {
          setApStatus(null);
          errors.push(actionErrorText(apResult.reason, "Setup AP status unavailable."));
        }
        setLoadError(errors.length > 0 ? errors[0] : null);
      })
      .finally(() => {
        if (ctrl.signal.aborted) return;
        setSnapshotLoading(false);
        setSnapshotRefreshing(false);
      });
  }, []);

  const reloadWifiViews = useCallback(async (signal: AbortSignal) => {
    const [nextStatus, nextNetworks, nextSaved] = await Promise.all([
      api.wifiStatus(signal),
      api.wifiNetworks(signal),
      api.wifiSaved(signal),
    ]);
    if (signal.aborted) return;
    setStatus(nextStatus);
    setNetworks(nextNetworks);
    setSaved(nextSaved);
  }, []);

  const reloadApView = useCallback(async (signal: AbortSignal) => {
    const next = await api.wifiApStatus(signal);
    if (signal.aborted) return;
    setApStatus(next.ap);
    setApModeDraft(toSafeApMode(next.ap.mode));
    if (!apEditingRef.current) setApSsidDraft(next.ap.ssid ?? "");
  }, []);

  useEffect(() => {
    reloadSnapshot(false);
    return () => {
      loadCtrl.current?.abort();
      wifiActionCtrl.current?.abort();
      apActionCtrl.current?.abort();
    };
  }, [reloadSnapshot]);

  useEffect(() => {
    if (!saved) {
      setPriorityOrder([]);
      return;
    }
    setPriorityOrder(saved.networks.map((network) => network.ssid));
  }, [saved]);

  const startWifiAction = () => {
    wifiActionCtrl.current?.abort();
    const ctrl = new AbortController();
    wifiActionCtrl.current = ctrl;
    return ctrl;
  };

  const startApAction = () => {
    apActionCtrl.current?.abort();
    const ctrl = new AbortController();
    apActionCtrl.current = ctrl;
    return ctrl;
  };

  const runScan = () => {
    if (scanBusy || wifiBusy || priorityBusy || apBusy) return;
    if (!confirmOperatorAction("Start Wi-Fi scan now?")) return;
    const ctrl = startWifiAction();
    setScanBusy(true);
    setNotice({ kind: "info", text: "Scanning nearby Wi-Fi networks…" });
    api.wifiScan(ctrl.signal)
      .then(async (nextNetworks) => {
        if (ctrl.signal.aborted) return;
        setNetworks(nextNetworks);
        await Promise.allSettled([reloadWifiViews(ctrl.signal), reloadApView(ctrl.signal)]);
        if (ctrl.signal.aborted) return;
        setNotice({ kind: "success", text: "Wi-Fi scan complete." });
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setNotice({
          kind: "error",
          text: actionErrorText(err, "Scan failed. Try again."),
        });
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setScanBusy(false);
      });
  };

  const runManualConnect = () => {
    if (wifiBusy || priorityBusy || apBusy) return;
    const ssid = manualSsid.trim();
    if (!ssid) {
      setNotice({ kind: "error", text: "SSID is required." });
      return;
    }
    if (!validPsk(manualPsk)) {
      setNotice({
        kind: "error",
        text: "Passphrase must be 8–63 chars or 64 hex chars.",
      });
      return;
    }
    if (!confirmOperatorAction(`Connect to Wi-Fi network "${ssid}" now?`)) return;
    const ctrl = startWifiAction();
    setWifiBusy(ssid);
    setNotice({ kind: "info", text: `Connecting to ${ssid}…` });
    const psk = manualPsk;
    api.wifiConnect(
      {
        ssid,
        ...(psk ? { psk } : {}),
      },
      ctrl.signal,
    )
      .then(async (resp) => {
        if (ctrl.signal.aborted) return;
        setNotice({
          kind: resp.connected ? "success" : "error",
          text: resp.connected
            ? `Connected to ${ssid}${resp.ip ? ` (${resp.ip})` : ""}`
            : `Could not connect to ${ssid}.`,
        });
        await Promise.allSettled([reloadWifiViews(ctrl.signal), reloadApView(ctrl.signal)]);
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setNotice({
          kind: "error",
          text: actionErrorText(err, `Could not connect to ${ssid}.`),
        });
      })
      .finally(() => {
        if (ctrl.signal.aborted) return;
        setWifiBusy(null);
        setManualPsk("");
      });
  };

  const runSelect = (ssid: string) => {
    if (wifiBusy || priorityBusy || apBusy) return;
    if (!confirmOperatorAction(`Switch to saved network "${ssid}" now?`)) return;
    const ctrl = startWifiAction();
    setWifiBusy(ssid);
    setNotice({ kind: "info", text: `Connecting to ${ssid}…` });
    api.wifiSelect(ssid, ctrl.signal)
      .then(async (resp) => {
        if (ctrl.signal.aborted) return;
        setNotice({
          kind: resp.connected ? "success" : "error",
          text: resp.connected
            ? `Connected to ${ssid}${resp.ip ? ` (${resp.ip})` : ""}`
            : `Could not connect to ${ssid}.`,
        });
        await Promise.allSettled([reloadWifiViews(ctrl.signal), reloadApView(ctrl.signal)]);
      })
      .catch(async (err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        const timeoutRollback =
          err instanceof ApiError && err.code === "wifi_select_timeout";
        setNotice({
          kind: "error",
          text: timeoutRollback
            ? `Could not connect to ${ssid} — previous network was kept.`
            : actionErrorText(err, `Could not connect to ${ssid}.`),
        });
        await Promise.allSettled([reloadWifiViews(ctrl.signal), reloadApView(ctrl.signal)]);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setWifiBusy(null);
      });
  };

  const runForget = (ssid: string, active: boolean) => {
    if (active || wifiBusy || priorityBusy || apBusy) return;
    if (!confirmOperatorAction(`Forget saved network "${ssid}"?`)) return;
    const ctrl = startWifiAction();
    setWifiBusy(ssid);
    setNotice({ kind: "info", text: `Forgetting ${ssid}…` });
    api.wifiForget(ssid, ctrl.signal)
      .then(async (resp) => {
        if (ctrl.signal.aborted) return;
        setNotice({
          kind: resp.forgotten ? "success" : "error",
          text: resp.forgotten
            ? `Forgot ${ssid}.`
            : `Could not forget ${ssid}.`,
        });
        await Promise.allSettled([reloadWifiViews(ctrl.signal), reloadApView(ctrl.signal)]);
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setNotice({
          kind: "error",
          text: actionErrorText(err, `Could not forget ${ssid}.`),
        });
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setWifiBusy(null);
      });
  };

  const movePriority = (index: number, direction: -1 | 1) => {
    if (priorityBusy || wifiBusy || apBusy) return;
    setPriorityOrder((prev) => {
      const next = prev.slice();
      const target = index + direction;
      if (target < 0 || target >= next.length) return prev;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const savePriorityOrder = () => {
    if (priorityBusy || wifiBusy || apBusy || !saved) return;
    if (!confirmOperatorAction("Apply this saved-network priority order?")) return;
    const ctrl = startWifiAction();
    setPriorityBusy(true);
    setNotice({ kind: "info", text: "Saving priority order…" });
    api.wifiPriority({ order: priorityOrder }, ctrl.signal)
      .then(async () => {
        if (ctrl.signal.aborted) return;
        setNotice({ kind: "success", text: "Priority order saved." });
        await Promise.allSettled([reloadWifiViews(ctrl.signal), reloadApView(ctrl.signal)]);
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setNotice({
          kind: "error",
          text: actionErrorText(err, "Could not save priority order."),
        });
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setPriorityBusy(false);
      });
  };

  const applyApMode = () => {
    if (apBusy || !apStatus) return;
    if (apModeDraft === apStatus.mode) {
      setNotice({ kind: "info", text: "AP mode is unchanged." });
      return;
    }
    if (
      !confirmOperatorAction(
        `Set setup AP mode to "${apModeDraft.replace("_", " ")}"?`,
      )
    ) {
      return;
    }
    const ctrl = startApAction();
    setApBusy(true);
    setNotice({ kind: "info", text: "Updating setup AP mode…" });
    api.wifiApMode(apModeDraft, ctrl.signal)
      .then(async () => {
        if (ctrl.signal.aborted) return;
        await reloadApView(ctrl.signal);
        setNotice({ kind: "success", text: "Setup AP mode updated." });
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setNotice({
          kind: "error",
          text: actionErrorText(err, "Could not update setup AP mode."),
        });
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setApBusy(false);
      });
  };

  const saveApConfig = () => {
    if (apBusy) return;
    const ssid = apSsidDraft.trim();
    if (!ssid) {
      setNotice({ kind: "error", text: "Setup AP name is required." });
      return;
    }
    if (apPassphraseDraft.length < 8 || apPassphraseDraft.length > 63) {
      setNotice({ kind: "error", text: "Setup AP password must be 8–63 chars." });
      return;
    }
    if (!confirmOperatorAction(`Save setup AP credentials for "${ssid}"?`)) return;
    const ctrl = startApAction();
    setApBusy(true);
    setNotice({ kind: "info", text: "Saving setup AP settings…" });
    api.wifiApConfig({ ssid, passphrase: apPassphraseDraft }, ctrl.signal)
      .then(async () => {
        if (ctrl.signal.aborted) return;
        setApEditing(false);
        setApPassphraseDraft("");
        await reloadApView(ctrl.signal);
        setNotice({ kind: "success", text: "Setup AP settings saved." });
      })
      .catch((err) => {
        if (ctrl.signal.aborted || isAbortError(err)) return;
        setNotice({
          kind: "error",
          text: actionErrorText(err, "Could not save setup AP settings."),
        });
      })
      .finally(() => {
        if (ctrl.signal.aborted) return;
        setApBusy(false);
        setApPassphraseDraft("");
      });
  };

  const connected = status?.connected === true;
  const statusChipClass = connected
    ? "captive-status-chip is-online"
    : "captive-status-chip is-offline";
  const savedBySsid = useMemo(() => {
    const map = new Map<string, SavedWifiNetwork>();
    for (const network of saved?.networks ?? []) map.set(network.ssid, network);
    return map;
  }, [saved]);
  const orderedSaved = useMemo(
    () =>
      priorityOrder
        .map((ssid) => savedBySsid.get(ssid))
        .filter((network): network is SavedWifiNetwork => network != null),
    [priorityOrder, savedBySsid],
  );
  const serverOrder = (saved?.networks ?? []).map((network) => network.ssid);
  const orderDirty = priorityOrder.join("\u0001") !== serverOrder.join("\u0001");
  const pageBusy =
    snapshotRefreshing || snapshotLoading || scanBusy || wifiBusy != null || priorityBusy || apBusy;

  return (
    <div class="container" data-page="captive-portal" data-screen="captive-portal">
      <section class="captive-page" aria-label="Wi-Fi setup">
        <header class="captive-hero">
          <div class="captive-hero-top">
            <div class="captive-list">
              <div class="captive-badge">
                <Icon name="wifi" class="captive-inline-icon" />
                <span>Wi-Fi setup</span>
              </div>
              <h1 class="captive-title">Connect TeslaUSB to Wi-Fi</h1>
              <p class="captive-copy">
                Scan nearby networks, join a saved network, or keep the setup
                access point online while you finish setup.
              </p>
            </div>
          </div>
          <div
            class="captive-banner is-warning"
            role="status"
            data-testid="captive-banner"
          >
            <div class="captive-badge">
              <Icon name="alert-triangle" class="captive-inline-icon" />
              <span>Operator-confirmed actions</span>
            </div>
            <p class="captive-copy">
              Wi-Fi onboarding can interrupt access. Actions here always require
              explicit confirmation and still enforce same-origin + server-side
              validation.
            </p>
          </div>
        </header>
        {notice ? (
          <p
            class={`captive-action-status is-${notice.kind}`}
            role="status"
            aria-live="polite"
            data-testid="captive-action-status"
          >
            {notice.text}
          </p>
        ) : null}
        {loadError ? (
          <div class="captive-banner is-warning" data-testid="captive-load-error">
            <p class="captive-copy">{loadError}</p>
            <button
              class="btn btn-secondary"
              type="button"
              onClick={() => reloadSnapshot(true)}
              disabled={snapshotRefreshing}
              data-testid="captive-retry-load"
            >
              {snapshotRefreshing ? "Retrying…" : "Retry"}
            </button>
          </div>
        ) : null}

        <section class="captive-grid">
          <div class="captive-list">
            <article class="captive-card" aria-labelledby="wifi-status-heading">
              <div class="captive-card-head">
                <div>
                  <h2 class="captive-card-title" id="wifi-status-heading">
                    Current status
                  </h2>
                  <p class="captive-card-copy">
                    Live connection details for the wireless client.
                  </p>
                </div>
                <div class={statusChipClass}>
                  <Icon name={connected ? "check-circle" : "alert-circle"} class="captive-inline-icon" />
                  <span>
                    {snapshotLoading && !status
                      ? "Loading…"
                      : connected
                        ? "Connected"
                        : "Not connected"}
                  </span>
                </div>
              </div>
              <div class="captive-status-list">
                <div class="captive-stat-card">
                  <div class="captive-stat-row">
                    <strong>SSID</strong>
                    <span>{status?.ssid ?? "None"}</span>
                  </div>
                  <p class="captive-status-copy">
                    Signal {status?.signal != null ? `${status.signal}%` : "Unknown"}
                  </p>
                </div>
                <div class="captive-stat-card">
                  <div class="captive-stat-row">
                    <strong>IP address</strong>
                    <span>{status?.ip ?? "Unavailable"}</span>
                  </div>
                  <p class="captive-status-copy">
                    {saved ? `${saved.networks.length} saved network(s)` : "Saved networks —"}
                  </p>
                </div>
                <div class="captive-stat-card">
                  <div class="captive-stat-row">
                    <strong>Interface</strong>
                    <span>{status?.iface ?? "Unavailable"}</span>
                  </div>
                  <p class="captive-status-copy">Security {status?.security ?? "Unknown"}</p>
                </div>
              </div>
              <div class="captive-button-row">
                <button
                  class="btn btn-secondary"
                  type="button"
                  onClick={() => reloadSnapshot(true)}
                  disabled={snapshotRefreshing}
                  data-testid="captive-refresh"
                >
                  {snapshotRefreshing ? "Refreshing…" : "Refresh data"}
                </button>
                <button
                  class="btn btn-primary"
                  type="button"
                  onClick={runScan}
                  disabled={pageBusy}
                  data-testid="captive-scan"
                >
                  {scanBusy ? "Scanning…" : "Scan now"}
                </button>
              </div>
            </article>

            <article class="captive-card" aria-labelledby="wifi-networks-heading">
              <div class="captive-card-head">
                <div>
                  <h2 class="captive-card-title" id="wifi-networks-heading">
                    Available networks
                  </h2>
                  <p class="captive-card-copy">
                    Select a visible network or enter the SSID manually for
                    hidden networks.
                  </p>
                </div>
              </div>
              {networks && networks.networks.length > 0 ? (
                <div class="captive-network-list" data-testid="captive-networks-list">
                  {networks.networks.map((network) => (
                    <div class="captive-network-item" key={network.ssid}>
                      <div class="captive-network-head">
                        <strong class="captive-network-name">{network.ssid || "Hidden network"}</strong>
                        <div class="captive-network-meta">
                          {network.active ? <span class="captive-pill">Active</span> : null}
                          {network.saved ? <span class="captive-pill">Saved</span> : null}
                          {network.protected ? <span class="captive-pill">Secured</span> : null}
                        </div>
                      </div>
                      <p class="captive-network-copy">
                        {network.signal}% · {network.security || "Open"}
                      </p>
                      <div class="captive-network-actions">
                        <button
                          class="btn btn-secondary"
                          type="button"
                          disabled={pageBusy}
                          data-testid="captive-use-ssid"
                          onClick={() => {
                            setManualSsid(network.ssid);
                            if (!network.protected) setManualPsk("");
                          }}
                        >
                          Use SSID
                        </button>
                        {network.saved && !network.active ? (
                          <button
                            class="btn btn-primary"
                            type="button"
                            disabled={pageBusy}
                            data-testid="captive-connect-saved-network"
                            onClick={() => runSelect(network.ssid)}
                          >
                            Connect saved
                          </button>
                        ) : null}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div class="captive-empty" data-testid="captive-networks-empty">
                  <Icon name="search" class="captive-icon" />
                  <h3 class="captive-empty-title">No networks listed</h3>
                  <p class="captive-empty-copy">
                    {loadError ?? "No networks were found by the Wi-Fi service."}
                  </p>
                </div>
              )}
            </article>
          </div>

          <div class="captive-list">
            <article class="captive-card" aria-labelledby="wifi-manual-heading">
              <div class="captive-card-head">
                <div>
                  <h2 class="captive-card-title" id="wifi-manual-heading">
                    Manual connection
                  </h2>
                  <p class="captive-card-copy">
                    Use this form for hidden SSIDs or when you want to override a
                    saved passphrase.
                  </p>
                </div>
              </div>
              <div class="captive-form">
                <div class="captive-form-row">
                  <label class="captive-form-label" for="manual-ssid">
                    SSID
                  </label>
                  <input
                    id="manual-ssid"
                    type="text"
                    maxLength={32}
                    value={manualSsid}
                    onInput={(event) => setManualSsid((event.target as HTMLInputElement).value)}
                    aria-label="Wi-Fi network name"
                    data-testid="captive-manual-ssid"
                  />
                </div>
                <div class="captive-form-row">
                  <label class="captive-form-label" for="manual-passphrase">
                    Passphrase
                  </label>
                  <input
                    id="manual-passphrase"
                    type="password"
                    value={manualPsk}
                    onInput={(event) => setManualPsk((event.target as HTMLInputElement).value)}
                    maxLength={64}
                    aria-label="Wi-Fi passphrase"
                    data-testid="captive-manual-psk"
                  />
                  <p class="captive-field-help">
                    Leave blank for open networks or to reuse a saved passphrase.
                  </p>
                </div>
                <div class="captive-form-actions">
                  <button
                    class="btn btn-primary"
                    type="button"
                    disabled={pageBusy || manualSsid.trim().length === 0}
                    data-testid="captive-connect-manual"
                    onClick={runManualConnect}
                  >
                    Connect now
                  </button>
                </div>
              </div>
            </article>

            <article class="captive-card" aria-labelledby="wifi-saved-heading">
              <div class="captive-card-head">
                <div>
                  <h2 class="captive-card-title" id="wifi-saved-heading">
                    Saved networks
                  </h2>
                  <p class="captive-card-copy">
                    Reconnect quickly or forget credentials that should no longer
                    be stored.
                  </p>
                </div>
              </div>
              {saved && orderedSaved.length > 0 ? (
                <div class="captive-network-list" data-testid="captive-saved-list">
                  {orderedSaved.map((network, index) => (
                    <div class="captive-network-item captive-saved-item" key={network.ssid}>
                      <div class="captive-network-head">
                        <strong>{network.ssid}</strong>
                        <span>{network.active ? "Connected" : "Saved"}</span>
                      </div>
                      <p class="captive-network-copy">
                        Priority {network.priority} ·{" "}
                        {network.autoconnect ? "Autoconnect on" : "Autoconnect off"}
                      </p>
                      <div class="captive-saved-actions">
                        {!network.active ? (
                          <button
                            class="btn btn-primary"
                            type="button"
                            disabled={pageBusy}
                            data-testid="captive-saved-connect"
                            onClick={() => runSelect(network.ssid)}
                          >
                            Connect
                          </button>
                        ) : null}
                        <button
                          class="btn btn-secondary"
                          type="button"
                          disabled={pageBusy || network.active}
                          data-testid="captive-saved-forget"
                          onClick={() => runForget(network.ssid, network.active)}
                        >
                          Forget
                        </button>
                        <div class="captive-reorder-actions">
                          <button
                            class="btn btn-secondary"
                            type="button"
                            disabled={pageBusy || index === 0}
                            aria-label={`Move ${network.ssid} up`}
                            data-testid="captive-priority-up"
                            onClick={() => movePriority(index, -1)}
                          >
                            ↑
                          </button>
                          <button
                            class="btn btn-secondary"
                            type="button"
                            disabled={pageBusy || index === orderedSaved.length - 1}
                            aria-label={`Move ${network.ssid} down`}
                            data-testid="captive-priority-down"
                            onClick={() => movePriority(index, 1)}
                          >
                            ↓
                          </button>
                        </div>
                      </div>
                    </div>
                  ))}
                  {orderedSaved.length > 1 ? (
                    <div class="captive-saved-actions">
                      <button
                        class="btn btn-primary"
                        type="button"
                        disabled={pageBusy || !orderDirty}
                        data-testid="captive-priority-save"
                        onClick={savePriorityOrder}
                      >
                        Save priority order
                      </button>
                      <button
                        class="btn btn-secondary"
                        type="button"
                        disabled={pageBusy || !orderDirty}
                        data-testid="captive-priority-reset"
                        onClick={() => setPriorityOrder(serverOrder)}
                      >
                        Reset
                      </button>
                    </div>
                  ) : null}
                </div>
              ) : (
                <div class="captive-empty" data-testid="captive-saved-empty">
                  <Icon name="folder" class="captive-icon" />
                  <h3 class="captive-empty-title">No saved Wi-Fi profiles</h3>
                  <p class="captive-empty-copy">
                    Profiles are stored after a successful connection.
                  </p>
                </div>
              )}
            </article>

            <article class="captive-card" aria-labelledby="wifi-ap-heading">
              <div class="captive-card-head">
                <div>
                  <h2 class="captive-card-title" id="wifi-ap-heading">
                    Setup access point
                  </h2>
                  <p class="captive-card-copy">
                    Configure AP mode and setup credentials using the existing AP APIs.
                  </p>
                  <p class="captive-card-copy">
                    For operator safety, force-off mode is not exposed on this page.
                  </p>
                </div>
              </div>
              {apStatus ? (
                <div class="captive-list">
                  <div class="captive-stat-card" data-testid="captive-ap-status">
                    <div class="captive-stat-row">
                      <strong>SSID</strong>
                      <span>{apStatus.ssid ?? "Not configured"}</span>
                    </div>
                    <p class="captive-status-copy">
                      {apStatus.active
                        ? `Active · ${apStatus.client_count} client(s)${
                          apStatus.ip ? ` · ${apStatus.ip}` : ""
                        }`
                        : "Inactive"}
                    </p>
                  </div>
                  <div class="captive-form-row">
                    <label class="captive-form-label" for="captive-ap-mode">
                      AP mode
                    </label>
                    <select
                      id="captive-ap-mode"
                      value={apModeDraft}
                      disabled={apBusy || snapshotLoading}
                      onChange={(event) =>
                        setApModeDraft((event.target as HTMLSelectElement).value as SafeApMode)
                      }
                      data-testid="captive-ap-mode"
                    >
                      <option value="auto">Auto (recommended)</option>
                      <option value="force_on">Force on</option>
                    </select>
                  </div>
                  <div class="captive-saved-actions">
                    <button
                      class="btn btn-primary"
                      type="button"
                      disabled={apBusy || snapshotLoading || apModeDraft === apStatus.mode}
                      onClick={applyApMode}
                      data-testid="captive-ap-mode-save"
                    >
                      Apply AP mode
                    </button>
                  </div>
                  <div class="captive-saved-actions">
                    <button
                      class="btn btn-secondary"
                      type="button"
                      disabled={apBusy || snapshotLoading}
                      onClick={() => {
                        if (!apEditing) {
                          setApSsidDraft(apStatus.ssid ?? "");
                          setApPassphraseDraft("");
                        }
                        setApEditing((prev) => !prev);
                      }}
                      data-testid="captive-ap-toggle-edit"
                    >
                      {apEditing ? "Hide AP credentials form" : "Edit AP credentials"}
                    </button>
                  </div>
                  {apEditing ? (
                    <div class="captive-form">
                      <div class="captive-form-row">
                        <label class="captive-form-label" for="captive-ap-ssid">
                          AP name
                        </label>
                        <input
                          id="captive-ap-ssid"
                          type="text"
                          value={apSsidDraft}
                          disabled={apBusy}
                          onInput={(event) =>
                            setApSsidDraft((event.target as HTMLInputElement).value)
                          }
                          data-testid="captive-ap-ssid"
                        />
                      </div>
                      <div class="captive-form-row">
                        <label class="captive-form-label" for="captive-ap-pass">
                          AP password
                        </label>
                        <input
                          id="captive-ap-pass"
                          type="password"
                          value={apPassphraseDraft}
                          disabled={apBusy}
                          maxLength={63}
                          onInput={(event) =>
                            setApPassphraseDraft((event.target as HTMLInputElement).value)
                          }
                          data-testid="captive-ap-pass"
                        />
                      </div>
                      <div class="captive-form-actions">
                        <button
                          class="btn btn-primary"
                          type="button"
                          disabled={apBusy}
                          onClick={saveApConfig}
                          data-testid="captive-ap-config-save"
                        >
                          Save AP credentials
                        </button>
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : (
                <div class="captive-empty" data-testid="captive-ap-unavailable">
                  <Icon name="alert-triangle" class="captive-icon" />
                  <h3 class="captive-empty-title">Setup AP unavailable</h3>
                  <p class="captive-empty-copy">
                    AP status could not be loaded. Retry once the Wi-Fi service is ready.
                  </p>
                </div>
              )}
            </article>
          </div>
        </section>
      </section>
    </div>
  );
}
