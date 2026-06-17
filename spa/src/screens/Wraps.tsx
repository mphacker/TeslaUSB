import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { BulkDeleteBar } from "../components/BulkDeleteBar";
import { useScreenHook } from "../components/screenHook";
import { useFullWidthScreen } from "../hooks/useFullWidthScreen";
import { MediaUploadZone } from "../components/MediaUploadZone";
import { api } from "../api/client";
import { fmtBytes, useMediaCategory } from "../hooks/useMediaCategory";
import "../styles/wraps.css";

/**
 * Wraps screen (route `/wraps`).
 *
 * Reads `GET /api/wraps` on mount — PNG images under the root-level `Wraps/`
 * folder on p2 (the layout Tesla's Paint Shop reads).
 * Install (POST) and remove (DELETE) route through the gadgetd eject-handoff.
 */
export function Wraps() {
  useScreenHook("wraps");
  useFullWidthScreen();

  const cat = useMediaCategory({
    fetchList: api.wraps,
    install: api.installWrap,
    remove: api.removeWrap,
    bulkDelete: api.bulkDeleteWraps,
    accept: [".png"],
  });

  return (
    <div class="container media-page" data-page="wraps" data-screen="wraps">
      <MediaPills active="wraps" />

      <h2>Custom Wraps</h2>
      <p class="wraps-description">
        Custom PNG wraps for Tesla&apos;s 3D vehicle visualization in the Paint
        Shop.
      </p>

      {/* ── Tesla requirements info ── */}
      <div class="wraps-info-box" data-testid="wraps-requirements">
        <p>
          <strong>Tesla Wrap Requirements:</strong>
        </p>
        <ul>
          <li>
            <strong>Folder:</strong> <code>/Wraps</code> at the root
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
            <strong>Count:</strong> Up to 10 wraps at a time
          </li>
        </ul>
        <p class="wraps-usage">
          <strong>Usage:</strong> Wraps appear in Toybox → Paint Shop → Wraps
          tab.{" "}
          <a
            href="https://github.com/teslamotors/custom-wraps"
            target="_blank"
            rel="noopener"
          >
            Download templates from Tesla
          </a>
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
      <div class="wraps-folder-controls" id="wrapUploadControls">
        <MediaUploadZone
          cat={cat}
          testId="wraps-dropzone"
          accept=".png,image/png"
          icon="image"
          title="Choose PNG wraps (≤ 1 MB each)"
          hint="PNG files only • 512–1024px — drag & drop or pick multiple"
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

      {/* ── Wrap library ── */}
      <div class="wraps-table-container" data-testid="wraps-library">
        {cat.state.tag === "loading" && (
          <div role="status" aria-busy="true" data-testid="wraps-loading">Loading…</div>
        )}
        {cat.state.tag === "error" && (
          <div role="alert" data-testid="wraps-error">
            Couldn't load wraps.{" "}
            <button class="action-btn" onClick={cat.refetch}>Retry</button>
          </div>
        )}
        {cat.state.tag === "ready" && (
          <>
            <BulkDeleteBar cat={cat} noun="wraps" />
            <table class="wraps-table">
              <thead>
                <tr>
                  {cat.state.items.length > 0 && (
                    <th class="bulk-check-col" aria-label="Select"></th>
                  )}
                  <th class="wraps-preview-col">Preview</th>
                  <th class="wraps-filename-col">Filename</th>
                  <th class="wraps-size-col">Size</th>
                  <th class="wraps-actions-col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {cat.state.items.length === 0 ? (
                  <tr>
                    <td colSpan={4}>
                      <div class="wraps-empty" data-testid="wraps-empty">
                        <Icon name="palette" class="wraps-empty-icon" />
                        <p>No custom wraps installed yet.</p>
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
                            data-testid="wraps-thumb"
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
