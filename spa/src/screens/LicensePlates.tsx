import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { BulkDeleteBar } from "../components/BulkDeleteBar";
import { useScreenHook } from "../components/screenHook";
import { api } from "../api/client";
import { fmtBytes, useMediaCategory } from "../hooks/useMediaCategory";
import "../styles/license-plates.css";

/**
 * Custom License Plates screen (route `/license_plates`).
 *
 * Reads `GET /api/plates` on mount — PNG images under `LicensePlate/` on p2.
 * Install (POST) and remove (DELETE) route through the gadgetd eject-handoff.
 * PNG magic-byte validation is done server-side; client validates extension only.
 */
export function LicensePlates() {
  useScreenHook("plates");

  const cat = useMediaCategory({
    fetchList: api.plates,
    install: api.installPlate,
    remove: api.removePlate,
    bulkDelete: api.bulkDeletePlates,
  });

  return (
    <div class="container media-page" data-page="plates" data-screen="plates">
      <MediaPills active="plates" />

      <h2>Custom License Plates</h2>
      <p style="margin: 4px 0 0 0; color: var(--text-secondary); font-size: 14px;">
        Custom background images for the in-car license plate selector.
      </p>

      {/* ── Tesla requirements info ── */}
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
            <strong>Count:</strong> Up to 5 plates at a time
          </li>
        </ul>
        <p class="license-plates-info-note">
          <strong>Usage:</strong> License plates appear in the in-car Background
          &rarr; Image selector under license plate config.
        </p>
      </div>

      {/* ── Notice banner ── */}
      {cat.notice && (
        <div class="settings-section" role="status" style="color: var(--accent-success);">
          {cat.notice}{" "}
          <button class="action-btn" style="font-size:12px;padding:2px 8px;" onClick={cat.clearNotice}>Dismiss</button>
        </div>
      )}

      {/* ── Upload zone ── */}
      <form
        class="license-plates-drop-zone"
        onSubmit={cat.onUploadSubmit}
        aria-busy={cat.uploading}
        data-testid="license-plates-dropzone"
      >
        <div class="license-plates-drop-content">
          <Icon name="image" class="license-plates-drop-icon" />
          <p class="license-plates-drop-title">Choose a PNG file (≤ 512 KB)</p>
          <p class="license-plates-drop-hint">PNG only. Tesla output: 420x75 or 492x75.</p>
          <input
            ref={cat.fileInputRef}
            type="file"
            accept=".png,image/png"
            onChange={cat.onFileChange}
            disabled={cat.uploading}
            aria-label="Choose license plate PNG"
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
          style="margin-top: 8px;"
        >
          {cat.uploading ? "Installing…" : "Install"}
        </button>
      </form>

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

      {/* ── License plate library ── */}
      <div
        class="license-plates-table-container"
        data-testid="license-plates-library"
      >
        {cat.state.tag === "loading" && (
          <div role="status" aria-busy="true" data-testid="license-plates-loading">Loading…</div>
        )}
        {cat.state.tag === "error" && (
          <div role="alert" data-testid="license-plates-error">
            Couldn't load license plates.{" "}
            <button class="action-btn" onClick={cat.refetch}>Retry</button>
          </div>
        )}
        {cat.state.tag === "ready" && (
          <>
            <BulkDeleteBar cat={cat} noun="plates" />
            <table class="license-plates-table" style="table-layout: fixed;">
              <thead>
                <tr>
                  {cat.state.items.length > 0 && (
                    <th style="width: 6%;" aria-label="Select"></th>
                  )}
                  <th style="width: 18%;">Preview</th>
                  <th style="width: 34%;">Filename</th>
                  <th style="width: 16%;">Size</th>
                  <th style="width: 26%;">Actions</th>
                </tr>
              </thead>
              <tbody>
                {cat.state.items.length === 0 ? (
                  <tr>
                    <td colSpan={4}>
                      <div
                        class="license-plates-empty"
                        data-testid="license-plates-empty"
                      >
                        <Icon name="image" class="license-plates-empty-icon" />
                        <p>No custom license plates installed yet.</p>
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
                        <td>
                          <img
                            class="media-thumb"
                            src={api.mediaContentUrl(item.rel_path, item.modified)}
                            alt={item.name}
                            loading="lazy"
                            width={64}
                            height={64}
                            data-testid="plates-thumb"
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
