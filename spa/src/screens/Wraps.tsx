import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { useScreenHook } from "../components/screenHook";
import "../styles/wraps.css";

/**
 * Wraps screen (route `/wraps`, parity port of the legacy `wraps.html`).
 *
 * Tesla custom wraps are PNG images stored under `/LightShow/wraps` on the
 * media partition and shown in Toybox → Paint Shop → Wraps.
 *
 * B-1 reality: webd has NO toybox list/read/upload endpoint yet, and every
 * media mutation routes through the operator-gated gadgetd eject-handoff. This
 * screen reproduces the v1 LOOK faithfully but is strictly READ-ONLY: static
 * requirements render verbatim, the upload zone is an inert `.is-disabled`
 * div (no `<form>`, no file input, no submit — zero mutation surface), and the
 * library renders the v1 table shell with an honest pending note instead of
 * fabricated previews/actions. It makes NO API calls.
 */
export function Wraps() {
  useScreenHook("wraps");

  return (
    <div class="container media-page" data-page="wraps" data-screen="wraps">
      <MediaPills active="wraps" />

      <h2>Custom Wraps</h2>
      <p class="wraps-description">
        Custom PNG wraps for Tesla&apos;s 3D vehicle visualization in the Paint
        Shop.
      </p>

      {/* ── Tesla requirements info (ported from v1) ── */}
      <div class="wraps-info-box" data-testid="wraps-requirements">
        <p>
          <strong>Tesla Wrap Requirements:</strong>
        </p>
        <ul>
          <li>
            <strong>Folder:</strong> <code>/LightShow/wraps</code> at the root
            of the media partition
          </li>
          <li>
            <strong>Format:</strong> PNG only
          </li>
          <li>
            <strong>Size:</strong> 1 MB maximum
          </li>
          <li>
            <strong>Dimensions:</strong> 512x512 to 1024x1024 pixels
          </li>
          <li>
            <strong>Filename:</strong> 30 characters max (letters, numbers,
            underscores, dashes, spaces)
          </li>
          <li>
            <strong>Count:</strong> Up to 10 wraps at a time
          </li>
        </ul>
        <p class="wraps-usage">
          <strong>Usage:</strong> Wraps appear in Toybox → Paint Shop → Wraps
          tab. {" "}
          <a
            href="https://github.com/teslamotors/custom-wraps"
            target="_blank"
            rel="noopener"
          >
            Download templates from Tesla
          </a>
        </p>
      </div>

      {/* ── Upload visual (inert; uploads are operator-gated) ── */}
      <div class="wraps-folder-controls" id="wrapUploadControls">
        <div
          class="wraps-drop-zone is-disabled"
          aria-disabled="true"
          data-testid="wraps-dropzone"
        >
          <div class="wraps-drop-inner">
            <Icon name="image" class="wraps-drop-icon" />
            <p class="wraps-drop-title">Uploads are managed on the device</p>
            <p class="wraps-drop-hint">
              PNG files only • 512-1024px • Max 1 MB each
            </p>
            <p class="wraps-drop-note">
              Installing a custom wrap momentarily ejects the USB drive from
              the vehicle, so it stays an operator-gated maintenance action —
              not available from this always-on page.
            </p>
          </div>
        </div>
      </div>

      {/* ── Wrap library (no list endpoint → honest v1 table empty-state) ── */}
      <div class="wraps-table-container" data-testid="wraps-library">
        <table class="wraps-table">
          <thead>
            <tr>
              <th class="wraps-preview-col">Preview</th>
              <th class="wraps-filename-col">Filename</th>
              <th class="wraps-dimensions-col">Dimensions</th>
              <th class="wraps-size-col">Size</th>
              <th class="wraps-actions-col">Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td colSpan={5}>
                <div class="wraps-empty" data-testid="wraps-empty">
                  <Icon name="palette" class="wraps-empty-icon" />
                  <p>
                    The Custom Wraps library will list installed wraps once
                    webd can read the media partition. No wraps can be listed
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
