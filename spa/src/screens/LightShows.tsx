import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { BulkDeleteBar } from "../components/BulkDeleteBar";
import { useScreenHook } from "../components/screenHook";
import { useFullWidthScreen } from "../hooks/useFullWidthScreen";
import { MediaUploadZone } from "../components/MediaUploadZone";
import { api } from "../api/client";
import { fmtBytes, useMediaCategory } from "../hooks/useMediaCategory";
import "../styles/light-shows.css";

function isPlayableAudio(name: string): boolean {
  return /\.(mp3|wav)$/i.test(name);
}

/**
 * Light Shows screen (route `/light_shows`).
 *
 * Reads `GET /api/lightshows` on mount — rows under `LightShow/` on p2,
 * excluding the root-level `Wraps/` folder (which belongs to Wraps).
 * Install (POST) and remove (DELETE) route through the gadgetd eject-handoff.
 */
export function LightShows() {
  useScreenHook("light-shows");
  useFullWidthScreen();

  const cat = useMediaCategory({
    fetchList: api.lightshows,
    install: api.installLightshow,
    remove: api.removeLightshow,
    bulkDelete: api.bulkDeleteLightshows,
    accept: [".fseq", ".mp3", ".wav"],
  });

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

      {/* ── Requirements card ── */}
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
            <strong>Individual files:</strong> .fseq, .mp3, .wav (≤ 5 MB each)
          </li>
        </ul>
      </div>

      {/* ── Notice banner ── */}
      {cat.notice && (
        <div class="settings-section" role="status" style="color: var(--accent-success);">
          {cat.notice}{" "}
          <button class="action-btn" style="font-size:12px;padding:2px 8px;" onClick={cat.clearNotice}>Dismiss</button>
        </div>
      )}

      {/* ── Upload area ── */}
      <div class="light-shows-folder-controls">
        <MediaUploadZone
          cat={cat}
          testId="light-shows-dropzone"
          accept=".fseq,.mp3,.wav"
          icon="cloud-upload"
          title="Choose light show files (≤ 5 MB each)"
          hint="Supports: .fseq, .mp3, .wav — drag & drop or pick multiple"
        />
      </div>

      {/* ── Confirm remove dialog ── */}
      {cat.confirmRemoveName && (
        <div class="settings-section" role="dialog" aria-label="Confirm remove">
          <p>Remove <strong>{cat.confirmRemoveName}</strong>? This ejects the USB drive momentarily.</p>
          {cat.removeFail && (
            <p role="alert" style="color: var(--accent-error);">{cat.removeFail.message}</p>
          )}
          <button class="action-btn" onClick={cat.onConfirmRemove} disabled={cat.removing} aria-busy={cat.removing}>
            {cat.removing ? "Removing…" : "Remove"}
          </button>{" "}
          <button class="action-btn" onClick={cat.onCancelRemove} disabled={cat.removing}>Cancel</button>
        </div>
      )}

      {/* ── Light-show library ── */}
      <div class="light-shows-video-table-container" data-testid="light-shows-library">
        {cat.state.tag === "loading" && (
          <div role="status" aria-busy="true" data-testid="light-shows-loading">Loading…</div>
        )}
        {cat.state.tag === "error" && (
          <div role="alert" data-testid="light-shows-error">
            Couldn't load light shows.{" "}
            <button class="action-btn" onClick={cat.refetch}>Retry</button>
          </div>
        )}
        {cat.state.tag === "ready" && (
          <>
            <BulkDeleteBar cat={cat} noun="shows" />
            <table class="light-shows-video-table">
              <thead>
                <tr>
                  {cat.state.items.length > 0 && (
                    <th class="bulk-check-col" aria-label="Select"></th>
                  )}
                  <th class="show-name-column">Show Name</th>
                  <th class="show-files-column">Size</th>
                  <th class="show-play-column">Play</th>
                  <th class="show-actions-column">Actions</th>
                </tr>
              </thead>
              <tbody>
                {cat.state.items.length === 0 ? (
                  <tr>
                    <td colSpan={5}>
                      <div class="light-shows-empty" data-testid="light-shows-empty">
                        <Icon name="sparkles" class="empty-icon" />
                        <p>No light show files installed yet.</p>
                      </div>
                    </td>
                  </tr>
                ) : (
                  cat.state.items.map((item) => {
                    const checked = cat.selected.has(item.name);
                    return (
                      <tr key={item.rel_path} class={checked ? "media-row-selected" : undefined}>
                        <td>
                          <input
                            type="checkbox"
                            class="bulk-row-check"
                            checked={checked}
                            onChange={() => cat.toggleSelect(item.name)}
                            disabled={cat.bulkDeleting}
                            aria-label={`Select ${item.name}`}
                          />
                        </td>
                        <td>{item.name}</td>
                        <td>{fmtBytes(item.size_bytes)}</td>
                        <td>
                          {isPlayableAudio(item.name) ? (
                            <audio
                              class="media-row-player"
                              controls
                              preload="none"
                              data-testid="light-shows-audio"
                              src={api.mediaContentUrl(item.rel_path, item.modified)}
                            />
                          ) : (
                            <span aria-hidden="true">—</span>
                          )}
                        </td>
                        <td>
                          <button
                            class="action-btn"
                            onClick={() => cat.onRequestRemove(item.name)}
                            disabled={cat.removing}
                            aria-label={`Remove ${item.name}`}
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}
