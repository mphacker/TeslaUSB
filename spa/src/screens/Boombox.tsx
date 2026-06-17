import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { BulkDeleteBar } from "../components/BulkDeleteBar";
import { useScreenHook } from "../components/screenHook";
import { useFullWidthScreen } from "../hooks/useFullWidthScreen";
import { MediaUploadZone } from "../components/MediaUploadZone";
import { api } from "../api/client";
import { fmtBytes, useMediaCategory } from "../hooks/useMediaCategory";
import "../styles/boombox.css";

/**
 * Boombox screen (route `/boombox`).
 *
 * Reads `GET /api/boombox` on mount and displays the installed horn sounds.
 * Install (POST) and remove (DELETE) route through the gadgetd eject-handoff;
 * the upload form shows a busy state during the handoff and surfaces transient
 * vs terminal failures per the chimes classifier pattern. `.wav` and `.mp3` are
 * accepted (server validates the WAV header; client validates extension only).
 */
export function Boombox() {
  useScreenHook("boombox");
  useFullWidthScreen();

  const cat = useMediaCategory({
    fetchList: api.boombox,
    install: api.installBoombox,
    remove: api.removeBoombox,
    bulkDelete: api.bulkDeleteBoombox,
    accept: [".wav", ".mp3"],
  });

  return (
    <div class="container media-page" data-page="boombox" data-screen="boombox">
      <MediaPills active="boombox" />

      <h2>Boombox</h2>
      <p style="margin: 4px 0 0 0; color: var(--text-secondary); font-size: 14px;">
        Sounds Tesla plays through the external pedestrian-warning speaker.
      </p>

      {/* ── NHTSA safety warning ── */}
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

      {/* ── Requirements card ── */}
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
            <strong>Filename:</strong> ASCII letters, numbers, spaces, underscores,
            dashes, dots
          </li>
          <li>
            <strong>Count:</strong> Up to 5 sounds (Tesla loads the first 5
            alphabetically)
          </li>
        </ul>
      </div>

      {/* ── Notice banner ── */}
      {cat.notice && (
        <div class="settings-section" role="status" style="color: var(--accent-success);">
          {cat.notice}{" "}
          <button class="action-btn" style="font-size:12px;padding:2px 8px;" onClick={cat.clearNotice}>
            Dismiss
          </button>
        </div>
      )}

      {/* ── Add a sound ── */}
      <div class="boombox-section-header">
        <h3>Add a sound</h3>
        {cat.state.tag === "ready" && (
          <span class="boombox-count-badge">
            <Icon name="music" style="width: 14px; height: 14px;" />
            {cat.state.items.length} / 5 sounds
          </span>
        )}
      </div>

      <MediaUploadZone
        cat={cat}
        testId="boombox-dropzone"
        accept=".wav,.mp3"
        icon="upload"
        title="Choose WAV or MP3 files (≤ 1 MB each)"
        hint="Supports: .wav, .mp3 — drag & drop or pick multiple"
      />

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

      {/* ── Current sounds ── */}
      <div class="boombox-section-header">
        <h3>Current sounds</h3>
      </div>

      {cat.state.tag === "loading" && (
        <div role="status" aria-busy="true" data-testid="boombox-loading">Loading…</div>
      )}
      {cat.state.tag === "error" && (
        <div role="alert" data-testid="boombox-error">
          Couldn't load boombox sounds.{" "}
          <button class="action-btn" onClick={cat.refetch}>Retry</button>
        </div>
      )}
      {cat.state.tag === "ready" && cat.state.items.length === 0 && (
        <div class="boombox-empty" data-testid="boombox-empty">
          <Icon name="megaphone" class="empty-icon" />
          <p>No boombox sounds installed.</p>
        </div>
      )}
      {cat.state.tag === "ready" && cat.state.items.length > 0 && (
        <>
          <BulkDeleteBar cat={cat} noun="sounds" />
          <table class="settings-table" data-testid="boombox-list">
            <thead>
              <tr>
                <th class="bulk-check-col" aria-label="Select"></th>
                <th>Name</th>
                <th>Size</th>
                <th>Play</th>
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
                    <td>{item.name}</td>
                    <td>{fmtBytes(item.size_bytes)}</td>
                    <td>
                      <audio
                        class="media-row-player"
                        controls
                        preload="none"
                        data-testid="boombox-audio"
                        src={api.mediaContentUrl(item.rel_path, item.modified)}
                      />
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
              })}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
