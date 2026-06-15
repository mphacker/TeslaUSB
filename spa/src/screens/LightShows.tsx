import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { BulkDeleteBar } from "../components/BulkDeleteBar";
import { useScreenHook } from "../components/screenHook";
import { api } from "../api/client";
import { fmtBytes, useMediaCategory } from "../hooks/useMediaCategory";
import "../styles/light-shows.css";

/**
 * Light Shows screen (route `/light_shows`).
 *
 * Reads `GET /api/lightshows` on mount — rows under `LightShow/` on p2,
 * excluding the root-level `Wraps/` folder (which belongs to Wraps).
 * Install (POST) and remove (DELETE) route through the gadgetd eject-handoff.
 */
export function LightShows() {
  useScreenHook("light-shows");

  const cat = useMediaCategory({
    fetchList: api.lightshows,
    install: api.installLightshow,
    remove: api.removeLightshow,
    bulkDelete: api.bulkDeleteLightshows,
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
        <form
          class="light-shows-drop-zone"
          onSubmit={cat.onUploadSubmit}
          aria-busy={cat.uploading}
          data-testid="light-shows-dropzone"
        >
          <div class="light-shows-drop-zone-content">
            <Icon name="cloud-upload" class="drop-icon" />
            <p class="drop-title">Choose a light show file (≤ 5 MB)</p>
            <p class="drop-formats">Supports: .fseq, .mp3, .wav</p>
            <input
              ref={cat.fileInputRef}
              type="file"
              accept=".fseq,.mp3,.wav"
              onChange={cat.onFileChange}
              disabled={cat.uploading}
              aria-label="Choose light show file"
            />
            {cat.selectedFile && (
              <p>{cat.selectedFile.name} ({fmtBytes(cat.selectedFile.size)})</p>
            )}
          </div>
          {cat.uploadFail && (
            <p role="alert" style="color: var(--accent-error); margin: 8px 0;">
              {cat.uploadFail.message}
              {cat.uploadFail.retryable && (
                <> <button type="submit" class="action-btn" disabled={!cat.selectedFile}>Retry</button></>
              )}
            </p>
          )}
          <button
            type="submit"
            class="action-btn"
            disabled={!cat.selectedFile || cat.uploading}
            aria-busy={cat.uploading}
          >
            {cat.uploading ? "Installing…" : "Install"}
          </button>
        </form>
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
                  <th class="show-actions-column">Actions</th>
                </tr>
              </thead>
              <tbody>
                {cat.state.items.length === 0 ? (
                  <tr>
                    <td colSpan={3}>
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
