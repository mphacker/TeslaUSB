import { useEffect } from "preact/hooks";
import { MediaPills } from "../components/MediaPills";
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
 *  - webd exposes only `POST /api/chimes` (install/replace the single
 *    `LockChime.wav`) and `DELETE /api/chimes/{id}`, BOTH of which route
 *    through the gadgetd eject-handoff that momentarily ejects the USB drive
 *    from the live vehicle. That makes them operator-gated, so they are NOT
 *    wired into this always-on LAN page (a browser confirm is not a safety
 *    boundary); chime management stays a deliberate operator/hardware-rails
 *    action until proper arming/gating exists.
 *  - There is NO read endpoint to list chimes, report the active chime, or
 *    drive the scheduler/random-groups yet (the open "how does webd enumerate
 *    installed p2 media" design gate). So the data-dependent sections render
 *    honest "pending" states rather than inventing rows, players, or controls.
 *
 * The screen therefore makes NO API calls and is strictly read-only.
 */

function buildId(): string {
  return (
    (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__ ??
    "dev"
  );
}

export function Media() {
  // Wiring-proof hook: prove THIS module produced the live DOM (defends the
  // documented "edited JS the page never loaded" failure mode).
  useEffect(() => {
    (
      window as unknown as {
        __TESLAUSB_MEDIA_HOOKS__?: { build: string; screen: string };
      }
    ).__TESLAUSB_MEDIA_HOOKS__ = { build: buildId(), screen: "lock-chimes" };
  }, []);

  return (
    <div class="container media-page" data-page="media" data-screen="media">
      {/* ── Media pill sub-nav (v1 media_hub_nav.html parity) ── */}
      <MediaPills active="chimes" />

      <h2>Lock Chimes</h2>

      {/* ── Active Lock Chime ── (no read API yet → honest pending state) */}
      <div class="media-card" id="activeChimeSection">
        <h3>Active Lock Chime</h3>
        <p class="media-pending" data-testid="active-chime-pending">
          The active lock chime can’t be shown yet — webd doesn’t expose a media
          read endpoint in this build. It will appear here once the catalog
          indexes the media partition.
        </p>
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

      {/* ── Chime Library ── (no list endpoint → honest pending, no fake rows) */}
      <h3 class="media-library-heading">Chime Library</h3>
      <div class="media-card">
        <p class="media-pending" data-testid="library-pending">
          The chime library will list installed chimes once webd can enumerate
          the media partition. No chimes can be listed yet.
        </p>
      </div>
    </div>
  );
}
