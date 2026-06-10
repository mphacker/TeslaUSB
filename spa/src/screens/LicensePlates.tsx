import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { useScreenHook } from "../components/screenHook";
import "../styles/license-plates.css";

/**
 * Custom License Plates screen (route `/license_plates`, parity port of the
 * legacy `license_plates.html`).
 *
 * Tesla custom-background license-plate PNGs live in the `/LicensePlate`
 * folder at the root of the media (p2) partition. The v1 page is a media-pill
 * page with a requirements card, upload drop zone, and a table of installed
 * plates.
 *
 * B-1 reality: webd has NO toybox list/read/upload endpoint for this media
 * partition yet, and every media mutation routes through the operator-gated
 * gadgetd eject-handoff. So this screen reproduces the v1 LOOK faithfully but
 * is strictly READ-ONLY: static requirements render verbatim, the drop zone
 * renders in its inert `.is-disabled` state (no `<form>`, no file input, no
 * submit — zero mutation surface), and the library renders an honest pending
 * state instead of fabricated rows or actions. It makes NO API calls.
 */
export function LicensePlates() {
  useScreenHook("plates");

  return (
    <div class="container media-page" data-page="plates" data-screen="plates">
      <MediaPills active="plates" />

      <h2>Custom License Plates</h2>
      <p style="margin: 4px 0 0 0; color: var(--text-secondary); font-size: 14px;">
        Custom background images for the in-car license plate selector.
      </p>

      {/* ── Tesla requirements info (ported from v1) ── */}
      <div
        class="license-plates-info"
        data-testid="license-plates-requirements"
      >
        <p>
          <Icon name="info" class="license-plates-inline-icon" />
          <strong>Tesla License-Plate Requirements:</strong>
        </p>
        <ul>
          <li>
            <strong>Folder:</strong> <code>/LicensePlate</code> at the root of
            the media partition (managed for you)
          </li>
          <li>
            <strong>Format:</strong> PNG only
          </li>
          <li>
            <strong>Size:</strong> 512 KB maximum
          </li>
          <li>
            <strong>Dimensions:</strong> 420x75 (North America) or 492x75
            (Europe / Italy)
          </li>
          <li>
            <strong>Filename:</strong> 12 characters max, letters and numbers
            only (no spaces, dashes, or underscores)
          </li>
          <li>
            <strong>Count:</strong> Up to 5 plates at a time
          </li>
        </ul>
        <p class="license-plates-info-note">
          <strong>Usage:</strong> License plates appear in the in-car Background
          &rarr; Image selector under license plate config. <a
            href="https://github.com/teslamotors/custom-wraps"
            target="_blank"
            rel="noopener"
          >
            View Tesla's custom-wraps repository
          </a> (license-plate spec is documented in <a
            href="https://github.com/teslamotors/custom-wraps/issues/13"
            target="_blank"
            rel="noopener"
          >
            issue #13
          </a>).
        </p>
        <p class="license-plates-info-note">
          Drop any image and we'll open a cropper to produce a Tesla-compliant
          PNG &mdash; no need to resize ahead of time.
        </p>
      </div>

      {/* ── Upload zone visual (inert; uploads are operator-gated) ── */}
      <div
        class="license-plates-drop-zone is-disabled"
        aria-disabled="true"
        data-testid="license-plates-dropzone"
      >
        <div class="license-plates-drop-content">
          <Icon name="image" class="license-plates-drop-icon" />
          <p class="license-plates-drop-title">
            Uploads are managed on the device
          </p>
          <p class="license-plates-drop-hint">
            Installing a custom license plate momentarily ejects the USB drive
            from the vehicle, so it stays an operator-gated maintenance action —
            not available from this always-on page. Tesla output is PNG cropped
            to 420x75 or 492x75, max 512 KB.
          </p>
        </div>
      </div>

      {/* ── License plate library (no read endpoint → honest empty-state) ── */}
      <div
        class="license-plates-table-container"
        data-testid="license-plates-library"
      >
        <table class="license-plates-table" style="table-layout: fixed;">
          <thead>
            <tr>
              <th style="width: 18%;">Preview</th>
              <th style="width: 28%;">Filename</th>
              <th style="width: 14%;">Dimensions</th>
              <th style="width: 12%;">Size</th>
              <th style="width: 28%;">Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td colSpan={5}>
                <div
                  class="license-plates-empty"
                  data-testid="license-plates-empty"
                >
                  <Icon name="image" class="license-plates-empty-icon" />
                  <p>
                    The license-plate library will list installed plates once
                    webd can read the media partition. No plates can be listed
                    in this build yet.
                  </p>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
