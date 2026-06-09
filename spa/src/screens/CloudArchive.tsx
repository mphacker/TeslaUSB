import { Icon } from "../components/Icon";
import { useScreenHook } from "../components/screenHook";
import "../styles/cloud-archive.css";

/**
 * Cloud Archive screen (route `/cloud`, parity port of the legacy
 * `cloud_archive.html`).
 *
 * The v1 page configures rclone-backed cloud sync: a live sync-status banner,
 * synced/pending/failed/transferred stat cards, a provider-setup section, a
 * sync-settings form (folders, priority, reserve, retry, cleanup toggles), and
 * a sync queue + history.
 *
 * B-1 reality: uploadd owns cloud sync, but webd exposes NO cloud
 * read/config/queue endpoint yet (the `be-cloud-config` lane is still pending),
 * and connecting a provider / editing sync policy are privileged operator
 * actions. So this screen reproduces the v1 LOOK faithfully but is strictly
 * READ-ONLY: live counters degrade to an honest "—" pending state, the provider
 * and settings controls render inert (disabled, no `<form>`, no submit — zero
 * mutation surface), and the queue + history render their v1 empty-states. It
 * makes NO API calls.
 */
export function CloudArchive() {
  useScreenHook("cloud-archive");

  return (
    <div class="container" data-page="cloud-archive" data-screen="cloud-archive">
      {/* ── Section 1: Sync status (idle) ── */}
      <div class="device-status-card device-status-present" id="syncStatusCard">
        <div class="device-status-header">
          <span class="status-dot status-present" />
          <div class="device-status-info">
            <strong>Cloud Sync</strong>
            <p data-testid="cloud-sync-subtitle">
              Cloud sync status will appear here once webd exposes the uploadd
              queue. Configure a provider below to start syncing.
            </p>
          </div>
        </div>
        <div style="margin-top: 8px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
          <button
            type="button"
            class="edit-btn"
            disabled
            aria-disabled="true"
            title="Configure a cloud provider first"
            style="padding: 8px 16px; font-size: 14px;"
          >
            Sync Now
          </button>
          <span style="font-size: var(--text-sm); color: var(--text-secondary);">
            Set up a cloud provider below to start syncing.
          </span>
        </div>
      </div>

      {/* ── Sync statistics summary (honest pending) ── */}
      <div style="display: flex; gap: var(--space-4); flex-wrap: wrap; margin-bottom: var(--space-4);">
        {[
          ["Events Synced"],
          ["Events Pending"],
          ["Failed"],
          ["Transferred"],
        ].map(([label]) => (
          <div
            key={label}
            style="flex: 1; min-width: 120px; padding: var(--space-3); background: var(--bg-info); border-radius: var(--radius-md); text-align: center;"
          >
            <div style="font-size: var(--text-2xl); font-weight: 600; color: var(--text-primary);">
              &mdash;
            </div>
            <div style="font-size: var(--text-sm); color: var(--text-secondary);">
              {label}
            </div>
          </div>
        ))}
      </div>
      <div style="display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; margin: -8px 0 var(--space-4);">
        <p style="font-size: var(--text-xs); color: var(--text-muted); margin: 0;">
          Each event contains multiple camera files (up to 6 angles per clip).
        </p>
        <button
          type="button"
          disabled
          aria-disabled="true"
          title="Reset counters (operator-managed)"
          style="padding: 4px 12px; font-size: var(--text-xs); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-secondary); color: var(--text-secondary);"
        >
          Reset counters
        </button>
      </div>

      {/* ── Section 2: Cloud Provider (not connected) ── */}
      <details class="settings-section" open>
        <summary>
          <Icon name="cloud" class="nav-icon" />
          Cloud Provider
        </summary>
        <div class="section-content">
          <p style="margin: 0 0 var(--space-3); color: var(--text-secondary);">
            Set up a cloud storage provider to automatically archive dashcam
            footage. Provider setup runs on the device once webd exposes the
            uploadd configuration API.
          </p>
          <div style="margin-bottom: var(--space-3);">
            <label
              for="providerSelect"
              style="display: block; margin-bottom: var(--space-2); font-weight: 500; color: var(--text-primary);"
            >
              Provider:
            </label>
            <select
              id="providerSelect"
              disabled
              aria-disabled="true"
              style="width: 100%; max-width: 360px; padding: 10px; border-radius: 6px; border: 1px solid var(--border-input); font-size: 15px; background-color: var(--form-input-bg); color: var(--text-primary);"
            >
              <option value="">-- Select Provider --</option>
              <option value="google-drive">Google Drive</option>
              <option value="onedrive">OneDrive</option>
              <option value="dropbox">Dropbox</option>
              <option value="s3">Amazon S3</option>
              <option value="b2">Backblaze B2</option>
              <option value="wasabi">Wasabi</option>
              <option value="generic">
                NAS / Custom rclone (SFTP, WebDAV, SMB, FTP, ...)
              </option>
            </select>
          </div>
        </div>
      </details>

      {/* ── Section 3: Sync Settings (inert) ── */}
      <details class="settings-section" open>
        <summary>
          <Icon name="settings" class="nav-icon" />
          Sync Settings
        </summary>
        <div class="section-content">
          <div
            class="info-box"
            style="margin-bottom: var(--space-4); font-size: var(--text-sm); line-height: 1.6; color: var(--text-secondary);"
          >
            <strong style="color: var(--text-primary);">How sync works:</strong>
            <ul style="margin: 6px 0 0; padding-left: 18px;">
              <li>
                <strong>Automatic:</strong> Starts syncing whenever WiFi connects
              </li>
              <li>
                <strong>Events:</strong> Syncs Sentry and Saved event folders by
                default
              </li>
              <li>
                <strong>Recent (telemetry):</strong> Optionally syncs RecentClips
                files when the vehicle was moving (GPS/SEI data was recorded for
                the clip)
              </li>
              <li>
                <strong>Oldest first:</strong> Within each folder, preserves the
                most at-risk clips first
              </li>
              <li>
                <strong>Skip existing:</strong> Files already on cloud storage are
                never re-uploaded
              </li>
              <li>
                <strong>Manual:</strong> Click &ldquo;Sync Now&rdquo; at any time
              </li>
            </ul>
            <p style="margin: 8px 0 0;">
              All camera angles for each event are uploaded together. You can
              continue browsing the web interface during sync.
            </p>
          </div>

          {/* Folder selection (inert) */}
          <div style="margin-bottom: var(--space-4);">
            <label style="display: block; margin-bottom: var(--space-2); font-weight: 600; color: var(--text-primary);">
              Folders to sync:
            </label>
            <div style="display: flex; flex-direction: column; gap: var(--space-2);">
              {[
                ["SentryClips", "Sentry-triggered events (impacts, intrusions)", true],
                ["SavedClips", "Manually saved clips (horn-honk / on-screen save)", true],
                [
                  "RecentClips (only clips with GPS/SEI data)",
                  "Sync continuous-recording clips when the vehicle was moving or generated telemetry — clips with no recorded waypoints are skipped.",
                  false,
                ],
              ].map(([name, blurb, checked]) => (
                <label
                  key={name as string}
                  style="display: flex; align-items: flex-start; gap: var(--space-2); padding: 8px; border-radius: var(--radius-md);"
                >
                  <input
                    type="checkbox"
                    checked={checked as boolean}
                    disabled
                    aria-disabled="true"
                    style="width: 18px; height: 18px; accent-color: var(--btn-success-bg); margin-top: 2px;"
                  />
                  <span style="display: flex; flex-direction: column; gap: 2px;">
                    <span style="color: var(--text-primary); font-size: 15px; font-weight: 500;">
                      {name}
                    </span>
                    <span style="color: var(--text-muted); font-size: var(--text-xs);">
                      {blurb}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </div>

          {/* Priority order (inert) */}
          <div style="margin-bottom: var(--space-4);">
            <label style="display: block; margin-bottom: var(--space-2); font-weight: 600; color: var(--text-primary);">
              Upload priority:
            </label>
            <p style="margin: 0 0 var(--space-2); font-size: var(--text-sm); color: var(--text-secondary);">
              Use the arrows to reorder. Top item is synced first.
            </p>
            <ol style="margin: 0; padding-left: 0; color: var(--text-primary); list-style: none;">
              {["SentryClips", "SavedClips", "RecentClips"].map((folder, i) => (
                <li
                  key={folder}
                  style="padding: 8px 12px; font-size: 15px; display: flex; align-items: center; gap: 8px; background: var(--bg-secondary); border-radius: 6px; margin-bottom: 4px;"
                >
                  <span style="min-width: 20px; color: var(--text-muted); font-weight: 600;">
                    {i + 1}.
                  </span>
                  <span style="flex: 1;">{folder}</span>
                  <button
                    type="button"
                    aria-label="Move up"
                    disabled
                    style="padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-primary); color: var(--text-primary); font-size: 14px; min-width: 32px; min-height: 32px;"
                  >
                    &#9650;
                  </button>
                  <button
                    type="button"
                    aria-label="Move down"
                    disabled
                    style="padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-primary); color: var(--text-primary); font-size: 14px; min-width: 32px; min-height: 32px;"
                  >
                    &#9660;
                  </button>
                </li>
              ))}
            </ol>
          </div>

          {/* Cloud storage reserve (inert) */}
          <div style="margin-bottom: var(--space-4);">
            <label style="display: block; margin-bottom: var(--space-2); font-weight: 600; color: var(--text-primary);">
              Cloud storage reserve (GB)
            </label>
            <input
              type="number"
              value="5.0"
              min="0"
              max="100"
              step="0.5"
              disabled
              aria-disabled="true"
              style="width:120px; padding:6px 10px; border:1px solid var(--border); border-radius:6px; background:var(--bg-secondary); color:var(--text-primary);"
            />
            <p style="font-size: var(--text-xs); color: var(--text-muted); margin:4px 0 0">
              When &ldquo;Auto-delete old cloud videos&rdquo; is on, the sweeper
              keeps at least this much free space on the remote.
            </p>
          </div>

          {/* Retry attempts (inert) */}
          <div style="margin-bottom: var(--space-4);">
            <label style="display: block; margin-bottom: var(--space-2); font-weight: 600; color: var(--text-primary);">
              Retry attempts before giving up
            </label>
            <input
              type="number"
              value="5"
              min="1"
              max="20"
              step="1"
              disabled
              aria-disabled="true"
              style="width:120px; padding:6px 10px; border:1px solid var(--border); border-radius:6px; background:var(--bg-secondary); color:var(--text-primary);"
            />
            <p style="font-size: var(--text-xs); color: var(--text-muted); margin:4px 0 0">
              How many times to retry a failed upload before marking it
              permanently failed. Failed uploads can still be retried manually
              from the Failed Jobs page.
            </p>
          </div>

          {/* Advanced toggles (inert) */}
          <div style="margin-bottom: var(--space-3);">
            <label style="display:flex; align-items:center; gap:8px; margin-bottom:8px">
              <input type="checkbox" disabled aria-disabled="true" />
              <span>Auto-delete old cloud videos when storage is low</span>
            </label>
            <p style="font-size: var(--text-xs); color: var(--text-muted); margin:0 0 0 24px">
              Deletes the oldest cloud objects past the minimum retention until
              the reserve is met. Runs after every sync drain.
            </p>
          </div>
          <div style="margin-bottom: var(--space-3);">
            <label style="display:flex; align-items:center; gap:8px; margin-bottom:8px">
              <input type="checkbox" checked disabled aria-disabled="true" />
              <span>Keep clips until backed up to cloud</span>
            </label>
            <p style="font-size: var(--text-xs); color: var(--text-muted); margin:0 0 0 24px">
              Requires a connected cloud provider. Only clips with GPS or SEI
              telemetry are protected &mdash; videos with no telemetry are
              eligible for cleanup regardless of upload state.
            </p>
          </div>

          <button
            type="button"
            class="edit-btn"
            disabled
            aria-disabled="true"
            style="padding: 8px 20px; font-size: 14px; margin-top: var(--space-3);"
          >
            Save Settings
          </button>
        </div>
      </details>

      {/* ── Section 3.5: Sync Queue (pending) ── */}
      <details class="settings-section">
        <summary>
          <Icon name="list" class="nav-icon" />
          Sync Queue
        </summary>
        <div class="section-content">
          <div
            class="cloud-empty"
            data-testid="cloud-queue-empty"
            style="text-align: center; padding: var(--space-6) 0; color: var(--text-muted);"
          >
            <Icon
              name="list"
              class="nav-icon"
              style="width: 48px; height: 48px; opacity: 0.4; margin-bottom: var(--space-2);"
            />
            <p style="margin: 0;">
              The sync queue will list pending uploads once webd exposes the
              uploadd queue. No queue can be shown in this build yet.
            </p>
          </div>
        </div>
      </details>

      {/* ── Section 4: Sync History (empty) ── */}
      <details class="settings-section">
        <summary>
          <Icon name="bar-chart-2" class="nav-icon" />
          Sync History
        </summary>
        <div class="section-content">
          <div
            class="cloud-empty"
            data-testid="cloud-history-empty"
            style="text-align: center; padding: var(--space-8) 0; color: var(--text-muted);"
          >
            <Icon
              name="cloud"
              class="nav-icon"
              style="width: 48px; height: 48px; opacity: 0.4; margin-bottom: var(--space-2);"
            />
            <p style="margin: 0;">No sync sessions yet</p>
          </div>
        </div>
      </details>
    </div>
  );
}
