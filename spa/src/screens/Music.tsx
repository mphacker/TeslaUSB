import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { useScreenHook } from "../components/screenHook";
import "../styles/music.css";

/**
 * Music screen (route `/music`, parity port of the legacy `music.html`).
 *
 * Tesla only scans music inside `/Music` on the media partition. The v1 page is
 * a media-pill page with a storage summary, folder/file browser, and upload
 * card. B-1 reality: webd has NO music list/read/upload endpoint yet, and every
 * media mutation routes through the operator-gated gadgetd eject-handoff. So
 * this screen reproduces the v1 LOOK faithfully but is strictly READ-ONLY: the
 * static guidance and card scaffolding render, the drop zone is inert (no
 * `<form>`, no file input, no buttons, no handlers), and the browser renders an
 * honest pending state instead of fabricated rows. It makes NO API calls.
 */
export function Music() {
  useScreenHook("music");

  return (
    <div class="container media-page" data-page="music" data-screen="music">
      <MediaPills active="music" />

      <h2>Music Library</h2>

      <div class="music-info-box" data-testid="music-info-banner">
        <p>
          Tesla only scans music inside the <code>/Music</code> folder. This
          page always reads and uploads inside that folder; paths shown below are
          relative to <code>/Music</code>.
        </p>
      </div>

      <div id="music-page" class="music-layout" data-testid="music-layout">
        {/* ── File browser panel (no read endpoint → honest pending state) ── */}
        <div class="music-panel" data-testid="music-library-panel">
          <div class="music-summary" data-testid="music-stats">
            <div class="stat-item">
              <div class="stat-label">Used</div>
              <div class="stat-value">—</div>
            </div>
            <div class="stat-item">
              <div class="stat-label">Free</div>
              <div class="stat-value">—</div>
            </div>
            <div class="stat-item">
              <div class="stat-label">Files</div>
              <div class="stat-value">—</div>
            </div>
          </div>

          <div class="music-meter" aria-label="Usage">
            <span aria-hidden="true" />
          </div>

          <div class="music-breadcrumb">
            <span>
              <Icon name="folder" class="breadcrumb-icon" /> /Music
            </span>
          </div>

          <h3 class="music-files-heading">Files</h3>
          <div class="music-empty" data-testid="music-empty" role="status">
            <Icon name="music" class="empty-icon" />
            <p>
              The music library will list folders and files once webd can read
              the media partition. No music can be listed in this build yet.
            </p>
          </div>
        </div>

        {/* ── Upload panel (inert; uploads are operator-gated) ── */}
        <div class="music-panel" data-testid="music-upload-panel">
          <h3 class="music-panel-title">Upload</h3>
          <p class="music-upload-intro">
            Uploads are operator-gated maintenance actions because installing
            music momentarily ejects the USB drive from the vehicle. Target
            folder: <strong>/Music</strong>
          </p>

          <div
            class="music-drop-zone is-disabled"
            aria-disabled="true"
            data-testid="music-dropzone"
          >
            <Icon name="music" class="drop-icon" />
            <div class="drop-label">Uploads are managed on the device</div>
            <div class="drop-hint">
              Allowed: mp3, flac, wav, aac, m4a. Max 512 MiB per file.
            </div>
            <div class="drop-note">
              Uploading music is not available from this always-on page until
              webd has a read/write media endpoint and an operator-gated
              USB-eject handoff.
            </div>
          </div>

          <div class="music-readonly-note" data-testid="music-upload-note">
            Folder creation, upload, move, and delete controls are intentionally
            absent in this read-only build.
          </div>
        </div>
      </div>
    </div>
  );
}
