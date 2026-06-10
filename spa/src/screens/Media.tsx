import { useEffect, useState } from "preact/hooks";
import { MediaPills } from "../components/MediaPills";
import { api } from "../api/client";
import type { Chimes, InstalledChime } from "../api/types";
import "../styles/media.css";

/**
 * The Media section (route `/media`, Shell active "media").
 *
 * Parity target: the legacy Flask app's `/media/` 302-redirected to
 * `/lock_chimes/`, so the visible "media page" was the **Lock Chimes**
 * management screen — a media pill sub-nav (`media_hub_nav.html`:
 * Chimes/Music/Boombox/Shows/Wraps/Plates) over the lock-chime manager
 * (`lock_chimes.html`): an "Active Lock Chime" card, an "Upload New Chime"
 * panel, "Chime Scheduler" + "Random Chime Groups" panels, and a "Chime
 * Library" table. This screen reproduces that v1 look using the carried-over
 * legacy stylesheet (`/static/css/style.css`: `.media-pills`, `.media-pill`,
 * `.settings-section`, …) which the SPA already loads.
 *
 * Backend reality (B-1, intentionally honest — NOT fabricated):
 *  - `GET /api/chimes` (read-only) reports which lock chime is installed on the
 *    p2 MEDIA partition, routed through the scannerd→indexd→webd catalog (NOT
 *    the gadgetd eject-handoff). The "Active Lock Chime" and "Chime Library"
 *    sections render that live fact, degrading to honest empty/pending states
 *    (never fabricated rows) when nothing is installed or the catalog can't be
 *    read.
 *  - `POST /api/chimes` (install/replace `LockChime.wav`) and
 *    `DELETE /api/chimes/{id}` BOTH route through the gadgetd eject-handoff that
 *    momentarily ejects the USB drive from the live vehicle. That makes them
 *    operator-gated, so they are NOT wired into this always-on LAN page (a
 *    browser confirm is not a safety boundary); chime management stays a
 *    deliberate operator/hardware-rails action until proper arming/gating
 *    exists.
 *  - The scheduler / random-groups have no backend yet, so they render honest
 *    "pending" states rather than inventing controls.
 *
 * The only API call is the read-only `GET /api/chimes`.
 */

const DASH = "\u2014";

function buildId(): string {
  return (
    (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__ ??
    "dev"
  );
}

/** A lock-chime byte count → compact human string (KB for the sub-1-MiB chimes). */
function chimeSize(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return DASH;
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

/** Naive-local `YYYY-MM-DDThh:mm:ss` → a readable `YYYY-MM-DD hh:mm` (or "—"). */
function chimeModified(s: string | null | undefined): string {
  if (!s) return DASH;
  return s.replace("T", " ").slice(0, 16);
}

/** Fetch lifecycle of `GET /api/chimes`. */
type Status = "loading" | "ready" | "error";

export function Media() {
  const [status, setStatus] = useState<Status>("loading");
  const [installed, setInstalled] = useState<InstalledChime | null>(null);

  useEffect(() => {
    // Wiring-proof hook: prove THIS module produced the live DOM (defends the
    // documented "edited JS the page never loaded" failure mode).
    (
      window as unknown as {
        __TESLAUSB_MEDIA_HOOKS__?: { build: string; screen: string };
      }
    ).__TESLAUSB_MEDIA_HOOKS__ = { build: buildId(), screen: "lock-chimes" };

    const ctrl = new AbortController();
    api
      .chimes(ctrl.signal)
      .then((c: Chimes) => {
        setInstalled(c.installed);
        setStatus("ready");
      })
      .catch(() => {
        // Aborted on unmount → ignore; any other failure degrades to an honest
        // error note without logging (the zero-console UAT gate holds).
        if (!ctrl.signal.aborted) setStatus("error");
      });
    return () => ctrl.abort();
  }, []);

  return (
    <div class="container media-page" data-page="media" data-screen="media">
      {/* ── Media pill sub-nav (v1 media_hub_nav.html parity) ── */}
      <MediaPills active="chimes" />

      <h2>Lock Chimes</h2>

      {/* ── Active Lock Chime ── (live from GET /api/chimes) */}
      <div class="media-card" id="activeChimeSection">
        <h3>Active Lock Chime</h3>
        {status === "ready" && installed ? (
          <div class="active-chime" data-testid="active-chime">
            <div class="active-chime-name" data-testid="active-chime-name">
              {installed.name}
            </div>
            <div class="active-chime-meta">
              <span class="chime-pill">{chimeSize(installed.size_bytes)}</span>
              <span class="chime-pill">
                Installed {chimeModified(installed.modified)}
              </span>
            </div>
          </div>
        ) : status === "ready" ? (
          <p class="media-pending" data-testid="active-chime-none">
            No lock chime is installed. The vehicle will play its built-in chime
            until one is installed through the maintenance tooling.
          </p>
        ) : status === "error" ? (
          <p class="media-pending" data-testid="active-chime-error">
            The active lock chime couldn’t be read just now. It will appear here
            once the media catalog can be reached.
          </p>
        ) : (
          <p class="media-pending" data-testid="active-chime-loading">
            Reading the installed lock chime…
          </p>
        )}
      </div>

      {/* ── Upload New Chime ── (operator-gated mutation, not wired here) */}
      <details class="settings-section" id="chimeUploadControls">
        <summary>Upload New Chime</summary>
        <div class="section-content">
          <p class="media-pending" data-testid="upload-pending">
            Installing a lock chime momentarily ejects the USB drive from the
            vehicle, so it’s an operator-gated action and isn’t available from
            this page yet. Chimes can be installed through the maintenance
            tooling. Format: a finished 16-bit PCM WAV under 1&nbsp;MB.
          </p>
        </div>
      </details>

      {/* ── Chime Scheduler ── (not implemented in B-1) */}
      <details class="settings-section" id="chimeSchedulerSection">
        <summary>Chime Scheduler</summary>
        <div class="section-content">
          <p class="media-pending">
            Scheduled chime switching isn’t available in this build yet.
          </p>
        </div>
      </details>

      {/* ── Random Chime Groups ── (not implemented in B-1) */}
      <details class="settings-section" id="randomChimeGroupsSection">
        <summary>Random Chime Groups</summary>
        <div class="section-content">
          <p class="media-pending">
            Random chime groups aren’t available in this build yet.
          </p>
        </div>
      </details>

      {/* ── Chime Library ── (live: the one installed chime, else honest empty) */}
      <h3 class="media-library-heading">Chime Library</h3>
      <div class="media-card">
        {status === "ready" && installed ? (
          <table class="chime-library" data-testid="chime-library">
            <thead>
              <tr>
                <th>Name</th>
                <th>Size</th>
                <th>Installed</th>
              </tr>
            </thead>
            <tbody>
              <tr data-testid="chime-row" data-rel-path={installed.rel_path}>
                <td class="chime-cell-name">{installed.name}</td>
                <td>{chimeSize(installed.size_bytes)}</td>
                <td>{chimeModified(installed.modified)}</td>
              </tr>
            </tbody>
          </table>
        ) : status === "ready" ? (
          <p class="media-pending" data-testid="library-empty">
            No chimes are installed yet.
          </p>
        ) : status === "error" ? (
          <p class="media-pending" data-testid="library-error">
            The chime library couldn’t be read just now.
          </p>
        ) : (
          <p class="media-pending" data-testid="library-loading">
            Reading the chime library…
          </p>
        )}
      </div>
    </div>
  );
}
