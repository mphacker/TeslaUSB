import { Icon } from "../components/Icon";
import { useScreenHook } from "../components/screenHook";
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
 * B-1 reality: wifid owns Wi-Fi, but webd exposes NO Wi-Fi read/scan/connect
 * endpoint yet (the `be-wifi-config` / `be-captive-portal` lanes are still
 * pending), and joining a network is a privileged operator action. So this
 * screen reproduces the v1 LOOK faithfully but is strictly READ-ONLY: the live
 * status degrades to an honest "not connected / pending" state, the network
 * lists render their v1 empty-states, and every form is replaced by inert
 * disabled controls (no `<form>`, no submit, zero mutation surface). It makes
 * NO API calls.
 */
export function CaptivePortal() {
  useScreenHook("captive-portal");

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
              so it stays an operator-gated maintenance action. This always-on
              page shows status read-only — it can&rsquo;t scan or change the
              connection.
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
                <div class="captive-status-chip is-offline">
                  <Icon name="alert-circle" class="captive-inline-icon" />
                  <span>Not connected</span>
                </div>
              </div>
              <div class="captive-status-list">
                <div class="captive-stat-card">
                  <div class="captive-stat-row">
                    <strong>SSID</strong>
                    <span>None</span>
                  </div>
                  <p class="captive-status-copy">Signal Unknown</p>
                </div>
                <div class="captive-stat-card">
                  <div class="captive-stat-row">
                    <strong>IP address</strong>
                    <span>Unavailable</span>
                  </div>
                  <p class="captive-status-copy">Saved networks &mdash;</p>
                </div>
                <div class="captive-stat-card">
                  <div class="captive-stat-row">
                    <strong>Setup AP</strong>
                    <span>Offline</span>
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
              <div class="captive-empty" data-testid="captive-networks-empty">
                <Icon name="search" class="captive-icon" />
                <h3 class="captive-empty-title">No networks listed</h3>
                <p class="captive-empty-copy">
                  Live scanning will appear here once webd exposes the wifid
                  status API. No networks can be listed in this build yet.
                </p>
              </div>
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
              <div class="captive-empty" data-testid="captive-saved-empty">
                <Icon name="folder" class="captive-icon" />
                <h3 class="captive-empty-title">No saved Wi-Fi profiles</h3>
                <p class="captive-empty-copy">
                  Profiles are stored after a successful connection.
                </p>
              </div>
            </article>
          </div>
        </section>
      </section>
    </div>
  );
}
