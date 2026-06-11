import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { BulkDeleteBar } from "../components/BulkDeleteBar";
import { useScreenHook } from "../components/screenHook";
import { api } from "../api/client";
import { fmtBytes, useMediaCategory } from "../hooks/useMediaCategory";
import "../styles/music.css";

/**
 * Music screen (route `/music`).
 *
 * Reads `GET /api/music` on mount and displays files under `Music/` on p2.
 * Install (POST) and remove (DELETE) route through the gadgetd eject-handoff.
 */
export function Music() {
  useScreenHook("music");

  const cat = useMediaCategory({
    fetchList: api.music,
    install: api.installMusic,
    remove: api.removeMusic,
    bulkDelete: api.bulkDeleteMusic,
  });

  return (
    <div class="container media-page" data-page="music" data-screen="music">
      <MediaPills active="music" />

      <h2>Music Library</h2>

      <div class="music-info-box" data-testid="music-info-banner">
        <p>
          Tesla only scans music inside the <code>/Music</code> folder. This
          page always reads and uploads inside that folder.
        </p>
      </div>

      {/* ── Notice banner ── */}
      {cat.notice && (
        <div class="settings-section" role="status" style="color: var(--accent-success);">
          {cat.notice}{" "}
          <button class="action-btn" style="font-size:12px;padding:2px 8px;" onClick={cat.clearNotice}>Dismiss</button>
        </div>
      )}

      <div id="music-page" class="music-layout" data-testid="music-layout">
        {/* ── File browser panel ── */}
        <div class="music-panel" data-testid="music-library-panel">
          <div class="music-summary" data-testid="music-stats">
            <div class="stat-item">
              <div class="stat-label">Files</div>
              <div class="stat-value">
                {cat.state.tag === "ready" ? cat.state.items.length : "—"}
              </div>
            </div>
          </div>

          <div class="music-breadcrumb">
            <span>
              <Icon name="folder" class="breadcrumb-icon" /> /Music
            </span>
          </div>

          <h3 class="music-files-heading">Files</h3>

          {cat.state.tag === "loading" && (
            <div role="status" aria-busy="true" data-testid="music-loading">Loading…</div>
          )}
          {cat.state.tag === "error" && (
            <div role="alert" data-testid="music-error">
              Couldn't load music library.{" "}
              <button class="action-btn" onClick={cat.refetch}>Retry</button>
            </div>
          )}
          {cat.state.tag === "ready" && cat.state.items.length === 0 && (
            <div class="music-empty" data-testid="music-empty" role="status">
              <Icon name="music" class="empty-icon" />
              <p>No music files installed yet.</p>
            </div>
          )}
          {cat.state.tag === "ready" && cat.state.items.length > 0 && (
            <>
              <BulkDeleteBar cat={cat} noun="tracks" />
              <table class="settings-table" data-testid="music-list">
                <thead>
                  <tr>
                    <th class="bulk-check-col" aria-label="Select"></th>
                    <th>Path</th>
                    <th>Size</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {cat.state.items.map((item) => {
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
                        <td style="word-break: break-all;">{item.rel_path.replace(/^Music\//, "")}</td>
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
                  })}
                </tbody>
              </table>
            </>
          )}
        </div>

        {/* ── Upload panel ── */}
        <div class="music-panel" data-testid="music-upload-panel">
          <h3 class="music-panel-title">Upload</h3>
          <p class="music-upload-intro">
            Target folder: <strong>/Music</strong>. Installing music
            momentarily ejects the USB drive.
          </p>

          <form onSubmit={cat.onUploadSubmit} aria-busy={cat.uploading}>
            <div
              class="music-drop-zone"
              data-testid="music-dropzone"
            >
              <Icon name="music" class="drop-icon" />
              <div class="drop-label">Choose an audio file (≤ 10 MB)</div>
              <div class="drop-hint">Allowed: mp3, flac, wav, aac, m4a</div>
              <input
                ref={cat.fileInputRef}
                type="file"
                accept=".mp3,.flac,.wav,.aac,.m4a"
                onChange={cat.onFileChange}
                disabled={cat.uploading}
                aria-label="Choose music file"
              />
              {cat.selectedFile && (
                <div>{cat.selectedFile.name} ({fmtBytes(cat.selectedFile.size)})</div>
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
              style="margin-top: 8px;"
            >
              {cat.uploading ? "Installing…" : "Install"}
            </button>
          </form>
        </div>
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
    </div>
  );
}
