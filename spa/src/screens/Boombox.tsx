import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { useScreenHook } from "../components/screenHook";
import "../styles/boombox.css";

/**
 * Boombox screen (route `/boombox`, parity port of the legacy `boombox.html`).
 *
 * Tesla plays short sounds through the external pedestrian-warning speaker from
 * a `/Boombox` folder on the media partition. The v1 page is a media-pill page
 * with an NHTSA safety warning, a requirements card, an upload drop zone, and a
 * library of the current sounds.
 *
 * B-1 reality: webd has NO toybox list/read/upload endpoint yet (the
 * `be-toybox-endpoints` lane is blocked on the "how does webd enumerate p2
 * media without mounting it" design gate), and every media mutation routes
 * through the operator-gated gadgetd eject-handoff. So this screen reproduces
 * the v1 LOOK faithfully but is strictly READ-ONLY: the static guidance (the
 * warning + requirements) renders verbatim, the drop zone renders in its inert
 * `.is-disabled` state (no `<form>`, no file input, no submit — zero mutation
 * surface), and the library renders the v1 empty-state with an honest pending
 * note instead of fabricated rows. It makes NO API calls.
 */
export function Boombox() {
  useScreenHook("boombox");

  return (
    <div class="container media-page" data-page="boombox" data-screen="boombox">
      <MediaPills active="boombox" />

      <h2>Boombox</h2>
      <p style="margin: 4px 0 0 0; color: var(--text-secondary); font-size: 14px;">
        Sounds Tesla plays through the external pedestrian-warning speaker.
      </p>

      {/* ── NHTSA safety warning (verbatim from v1) ── */}
      <div class="boombox-nhtsa-warning" role="alert">
        <Icon name="alert-triangle" class="nhtsa-icon" />
        <div class="nhtsa-body">
          <p>
            <strong>Boombox sounds only play while the vehicle is in Park</strong>{" "}
            (NHTSA safety restriction, Feb 2022).
          </p>
          <p>
            Your vehicle must have an external pedestrian-warning speaker —
            built September 2019 or later for Model 3, Y, S, or X, or any
            Cybertruck.
          </p>
        </div>
      </div>

      {/* ── Requirements card (verbatim from v1) ── */}
      <div class="boombox-requirements">
        <p style="margin: 0; display: flex; align-items: center; gap: 8px;">
          <Icon name="info" style="width: 18px; height: 18px; flex-shrink: 0;" />
          <strong>Tesla Boombox Requirements</strong>
        </p>
        <ul>
          <li>
            <strong>Folder:</strong> <code>/Boombox</code> at the root of the
            media partition (managed for you)
          </li>
          <li>
            <strong>Format:</strong> MP3 or WAV only
          </li>
          <li>
            <strong>Size:</strong> 1 MB maximum (≤ 5 seconds recommended)
          </li>
          <li>
            <strong>Filename:</strong> letters, numbers, spaces, underscores,
            dashes, dots
          </li>
          <li>
            <strong>Count:</strong> Up to 5 sounds (Tesla loads the first 5
            alphabetically)
          </li>
        </ul>
      </div>

      {/* ── Add a sound (inert; uploads are operator-gated) ── */}
      <div class="boombox-section-header">
        <h3>Add a sound</h3>
        <span class="boombox-count-badge" aria-disabled="true">
          <Icon name="music" style="width: 14px; height: 14px;" />— / 5 sounds
        </span>
      </div>

      <div
        class="boombox-drop-zone is-disabled"
        aria-disabled="true"
        data-testid="boombox-dropzone"
      >
        <Icon name="upload" class="drop-icon" />
        <div class="drop-title">Uploads are managed on the device</div>
        <div class="drop-hint">
          Installing a Boombox sound momentarily ejects the USB drive from the
          vehicle, so it stays an operator-gated maintenance action — not
          available from this always-on page.
        </div>
      </div>

      {/* ── Current sounds (no list endpoint → honest v1 empty-state) ── */}
      <div class="boombox-section-header">
        <h3>Current sounds</h3>
      </div>
      <div class="boombox-empty" data-testid="boombox-empty">
        <Icon name="megaphone" class="empty-icon" />
        <p>
          The Boombox library will list installed sounds once webd can read the
          media partition. No sounds can be listed in this build yet.
        </p>
      </div>
    </div>
  );
}
