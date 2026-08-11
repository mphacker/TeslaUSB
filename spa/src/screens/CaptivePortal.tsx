import { Icon } from "../components/Icon";
import { useScreenHook } from "../components/screenHook";
import { ApiError, api } from "../api/client";
import type {
  SavedWifiResponse,
  WifiNetworksResponse,
  WifiStatus,
} from "../api/types";
import { useEffect, useState } from "preact/hooks";
import "../styles/captive-portal.css";

/**
 * Captive Portal / Wi-Fi setup screen (route `/captive-portal`, parity port of
 * the legacy `captive_portal.html`).
 *
 * The v1 page is the device's Wi-Fi onboarding surface: a status card, an
 * available-networks list, a manual-connection form, and a saved-networks list.
 * Every control POSTs to a `captive_portal.*` Flask route (toggle AP, connect,
 * disconnect, forget).
 *
 * B-1 reality: wifid owns Wi-Fi and webd exposes read-only status, discovered
 * network, and saved-profile endpoints. Joining, forgetting, scanning, and AP
 * changes remain privileged operator actions, so those controls stay inert.
 */
export function CaptivePortal() {
  useScreenHook("captive-portal");
  const [status, setStatus] = useState<WifiStatus | null>(null);
  const [networks, setNetworks] = useState<WifiNetworksResponse | null>(null);
  const [saved, setSaved] = useState<SavedWifiResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    Promise.allSettled([
      api.wifiStatus(ctrl.signal),
      api.wifiNetworks(ctrl.signal),
      api.wifiSaved(ctrl.signal),
    ]).then(([statusResult, networksResult, savedResult]) => {
      if (ctrl.signal.aborted) return;
      const errors: string[] = [];
      if (statusResult.status === "fulfilled") setStatus(statusResult.value);
      else errors.push(statusResult.reason instanceof ApiError ? statusResult.reason.message : "Wi-Fi status unavailable.");
      if (networksResult.status === "fulfilled") setNetworks(networksResult.value);
      else errors.push(networksResult.reason instanceof ApiError ? networksResult.reason.message : "Wi-Fi scan unavailable.");
      if (savedResult.status === "fulfilled") setSaved(savedResult.value);
      else errors.push(savedResult.reason instanceof ApiError ? savedResult.reason.message : "Saved Wi-Fi profiles unavailable.");
      setLoadError(errors.length > 0 ? errors[0] : null);
    });
    return () => ctrl.abort();
  }, []);

  const connected = status?.connected === true;
  const statusChipClass = connected ? "captive-status-chip is-online" : "captive-status-chip is-offline";

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
              <span>Setup is operator-managed</span>
            </div>
            <p class="captive-copy">
              Wi-Fi onboarding joins a network and restarts the wireless client,
              so it stays an operator-gated maintenance action. This page shows
              live status read-only — it can&rsquo;t change the connection.
            </p>
          </div>
        </header>

        <section class="captive-grid">
          <div class="captive-list">
            <article class="captive-card" aria-labelledby="wifi-status-heading">
              <div class="captive-card-head">
                <div>
                  <h2 class="captive-card-title" id="wifi-status-heading">
                    Current status
                  </h2>
                  <p class="captive-card-copy">
                    Live connection details for the wireless client and the
                    setup access point.
                  </p>
                </div>
                <div class={statusChipClass}>
                  <Icon name={connected ? "check-circle" : "alert-circle"} class="captive-inline-icon" />
                  <span>{connected ? "Connected" : "Not connected"}</span>
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
                    <strong>Setup AP</strong>
                    <span>{status ? "Available" : "Unknown"}</span>
                  </div>
                  <p class="captive-status-copy">SSID TeslaUSB</p>
                </div>
              </div>
              <div class="captive-button-row">
                <button class="btn btn-primary" type="button" disabled aria-disabled="true">
                  Enable AP
                </button>
                <button class="btn btn-secondary" type="button" disabled aria-disabled="true">
                  Disconnect Wi-Fi
                </button>
                <button class="btn btn-secondary" type="button" disabled aria-disabled="true">
                  Rescan
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
                      <strong>{network.ssid || "Hidden network"}</strong>
                      <span>{network.signal}% · {network.security}</span>
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
              <div class="captive-form" aria-disabled="true">
                <div class="captive-form-row">
                  <label class="captive-form-label" for="manual-ssid">
                    SSID
                  </label>
                  <input
                    id="manual-ssid"
                    type="text"
                    maxLength={32}
                    disabled
                    aria-label="Wi-Fi network name"
                  />
                </div>
                <div class="captive-form-row">
                  <label class="captive-form-label" for="manual-passphrase">
                    Passphrase
                  </label>
                  <input
                    id="manual-passphrase"
                    type="password"
                    disabled
                    aria-label="Wi-Fi passphrase"
                  />
                  <p class="captive-field-help">
                    Leave blank for open networks or to reuse a saved passphrase.
                  </p>
                </div>
                <div class="captive-form-actions">
                  <button
                    class="btn btn-primary"
                    type="button"
                    disabled
                    aria-disabled="true"
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
              {saved && saved.networks.length > 0 ? (
                <div class="captive-network-list" data-testid="captive-saved-list">
                  {saved.networks.map((network) => (
                    <div class="captive-network-item" key={network.ssid}>
                      <strong>{network.ssid}</strong>
                      <span>{network.active ? "Connected" : "Saved"}</span>
                    </div>
                  ))}
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
          </div>
        </section>
      </section>
    </div>
  );
}
