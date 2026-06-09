import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { useScreenHook } from "../components/screenHook";
import "../styles/light-shows.css";

/**
 * Light Shows screen (route `/light_shows`, parity port of the legacy
 * `light_shows.html`).
 *
 * B-1 reality: webd has no media-partition list/read/upload endpoint for light
 * shows yet, and media writes must go through the operator-gated USB
 * eject-handoff. This screen therefore preserves the v1 LOOK while remaining
 * strictly READ-ONLY: no forms, no file inputs, no action buttons, no event
 * handlers, no API calls, and no fabricated library rows.
 */
export function LightShows() {
  useScreenHook("light-shows");

  return (
    <div
      class="container media-page"
      data-page="light-shows"
      data-screen="light-shows"
    >
      <MediaPills active="shows" />

      <h2>Light Shows</h2>
      <p class="light-shows-intro">
        Upload and manage Tesla Light Show sequences for the LightShow folder.
      </p>

      {/* ── Requirements card (static v1 upload guidance) ── */}
      <div class="light-shows-requirements" data-testid="light-shows-requirements">
        <p class="light-shows-requirements-title">
          <Icon name="info" class="requirements-icon" />
          <strong>Tesla Light Show Requirements</strong>
        </p>
        <ul>
          <li>
            <strong>Folder:</strong> <code>/LightShow</code> at the root of the
            media partition
          </li>
          <li>
            <strong>Individual files:</strong> .fseq, .mp3, .wav
          </li>
          <li>
            <strong>ZIP files:</strong> Can contain multiple light show files in
            any folder structure
          </li>
          <li>
            <strong>Supported upload selection:</strong> .fseq, .mp3, .wav, .zip
            (can select multiple files)
          </li>
        </ul>
      </div>

      {/* ── Upload area (inert; installs are operator-gated) ── */}
      <div class="light-shows-folder-controls">
        <div
          class="light-shows-drop-zone is-disabled"
          aria-disabled="true"
          data-testid="light-shows-dropzone"
        >
          <div class="light-shows-drop-zone-content">
            <Icon name="cloud-upload" class="drop-icon" />
            <p class="drop-title">Uploads are managed on the device</p>
            <p class="drop-hint">
              Installing a light show momentarily ejects the USB drive from the
              vehicle, so it stays an operator-gated maintenance action — not
              available from this always-on page.
            </p>
            <p class="drop-formats">
              Supports: .fseq, .mp3, .wav, .zip (can select multiple files)
            </p>
          </div>
        </div>
      </div>

      {/* ── Light-show library (no list endpoint → honest pending state) ── */}
      <div class="light-shows-video-table-container" data-testid="light-shows-library">
        <table class="light-shows-video-table">
          <thead>
            <tr>
              <th class="show-name-column">Show Name</th>
              <th class="show-files-column">Files</th>
              <th class="show-actions-column">Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td colSpan={3}>
                <div class="light-shows-empty" data-testid="light-shows-empty">
                  <Icon name="sparkles" class="empty-icon" />
                  <p>
                    Light shows will be listed once webd can read the media
                    partition. No light show files can be listed in this build
                    yet.
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
