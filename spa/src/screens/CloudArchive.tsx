import { Icon } from "../components/Icon";
import { useScreenHook } from "../components/screenHook";
import { useEffect, useState } from "preact/hooks";
import { ApiError, api } from "../api/client";
import type {
  CloudHistoryPageResponse,
  CloudQueueItem,
  CloudQueuePageResponse,
  CloudCredentialProvider,
  CloudCredentialsResponse,
  CloudStatusResponse,
} from "../api/types";
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
 * B-1 reality: cloud-provider credentials plus a read-only queue/status slice
 * are live today. This page now calls `GET/POST/DELETE /api/cloud/credentials`
 * for Section 2 and the new read-only `GET /api/cloud/queue` +
 * `GET /api/cloud/history` surfaces for observability. Mutating sync controls
 * remain pending.
 */
export function CloudArchive() {
  useScreenHook("cloud-archive");
  const [provider, setProvider] = useState<CloudCredentialProvider>("drive");
  const [token, setToken] = useState("");
  const [creds, setCreds] = useState<CloudCredentialsResponse | null>(null);
  const [cloudStatus, setCloudStatus] = useState<CloudStatusResponse | null>(null);
  const [queue, setQueue] = useState<CloudQueuePageResponse | null>(null);
  const [history, setHistory] = useState<CloudHistoryPageResponse | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [saveBusy, setSaveBusy] = useState(false);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [message, setMessage] = useState<
    { kind: "info" | "success" | "error"; text: string } | null
  >({ kind: "info", text: "Loading cloud provider status…" });

  const providerLabel = (value: CloudCredentialProvider): string =>
    value === "drive"
      ? "Google Drive"
      : value === "onedrive"
        ? "OneDrive"
        : "Dropbox";

  const formatSavedAt = (epoch: number | null | undefined): string => {
    if (epoch == null || !Number.isFinite(epoch)) return "—";
    return new Date(epoch * 1000).toLocaleString();
  };

  const statusText = (state: CloudCredentialsResponse): string => {
    if (state.state === "not_configured") return "No cloud provider configured.";
    if (state.state === "configured" && state.provider) {
      return `Connected to ${providerLabel(state.provider)} — saved ${formatSavedAt(state.updated_at)}.`;
    }
    return "Stored cloud credentials cannot be decrypted on this hardware (the SD card was moved). Re-paste credentials to continue syncing.";
  };

  const formatBytes = (bytes: number | null | undefined): string => {
    if (bytes == null || !Number.isFinite(bytes)) return "—";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
  };

  const queueCount = (
    loadedQueue: CloudQueuePageResponse | null,
    states: string[],
  ): number | null => {
    if (!loadedQueue) return null;
    return loadedQueue.items.filter((item) => states.includes(item.state)).length;
  };

  const queueSubtitle = (): string => {
    if (queueError) return "Cloud queue is temporarily unavailable.";
    if (!queue) return "Loading cloud queue…";
    if (queue.items.length === 0) return "No uploads are queued right now.";
    const pending = queueCount(queue, ["queued", "in_progress"]) ?? 0;
    const failed = queueCount(queue, ["failed"]) ?? 0;
    if (failed > 0) {
      return `${pending} pending upload(s), ${failed} failed.`;
    }
    return `${pending} upload(s) queued.`;
  };

  const uploaderSubtitle = (): string => {
    if (!cloudStatus) return "Uploader status is loading…";
    if (!cloudStatus.configured) return "Uploader is not configured.";
    if (cloudStatus.uploader_state === "running") return "Uploader is running.";
    if (cloudStatus.uploader_state === "stopping") return "Uploader is stopping.";
    if (cloudStatus.uploader_state === "starting") return "Uploader is starting.";
    if (cloudStatus.uploader_state === "idle") return "Uploader is idle.";
    return `Uploader state: ${cloudStatus.uploader_state}.`;
  };

  const uploadedFromHistory = (loadedHistory: CloudHistoryPageResponse | null): number | null => {
    if (!loadedHistory) return null;
    return loadedHistory.items.filter((item) => item.outcome === "uploaded").length;
  };

  const transferredFromHistory = (
    loadedHistory: CloudHistoryPageResponse | null,
  ): number | null => {
    if (!loadedHistory) return null;
    return loadedHistory.items
      .filter((item) => item.outcome === "uploaded")
      .reduce((total, item) => total + item.size_bytes, 0);
  };

  useEffect(() => {
    const ctrl = new AbortController();
    setQueueError(null);
    setHistoryError(null);
    Promise.allSettled([
      api.cloudCredentials(ctrl.signal),
      api.cloudStatus(ctrl.signal),
      api.cloudQueue({}, ctrl.signal),
      api.cloudHistory({}, ctrl.signal),
    ])
      .then(([credsResult, statusResult, queueResult, historyResult]) => {
        if (ctrl.signal.aborted) return;
        if (credsResult.status === "fulfilled") {
          setCreds(credsResult.value);
          if (credsResult.value.provider) setProvider(credsResult.value.provider);
          setConfirmRemove(false);
          setMessage({ kind: "info", text: statusText(credsResult.value) });
        } else {
          const err = credsResult.reason;
          setMessage({
            kind: "error",
            text:
              err instanceof ApiError
                ? err.message
                : "Could not load cloud credentials.",
          });
        }
        if (statusResult.status === "fulfilled") {
          setCloudStatus(statusResult.value);
        } else if (statusResult.reason instanceof ApiError) {
          setMessage({ kind: "error", text: statusResult.reason.message });
        }
        if (queueResult.status === "fulfilled") {
          setQueue(queueResult.value);
        } else {
          const err = queueResult.reason;
          if (err instanceof ApiError) {
            setQueueError(err.message);
          } else {
            setQueueError("Could not load cloud queue.");
          }
        }
        if (historyResult.status === "fulfilled") {
          setHistory(historyResult.value);
        } else {
          const err = historyResult.reason;
          if (err instanceof ApiError) {
            setHistoryError(err.message);
          } else {
            setHistoryError("Could not load cloud history.");
          }
        }
      });
    return () => ctrl.abort();
  }, []);

  const refreshObservability = async () => {
    try {
      const loaded = await api.cloudQueue();
      setQueue(loaded);
      setQueueError(null);
    } catch (err) {
      if (err instanceof ApiError) setQueueError(err.message);
    }
    try {
      const loaded = await api.cloudHistory();
      setHistory(loaded);
      setHistoryError(null);
    } catch (err) {
      if (err instanceof ApiError) setHistoryError(err.message);
    }
  };

  const onSaveCredentials = async () => {
    if (saveBusy || token.trim().length === 0) return;
    setSaveBusy(true);
    setConfirmRemove(false);
    setMessage({ kind: "info", text: "Saving cloud credentials…" });
    try {
      const next = await api.saveCloudCredentials({ provider, token });
      setCreds(next);
      if (next.provider) setProvider(next.provider);
      setToken("");
      await refreshObservability();
      setMessage({ kind: "success", text: statusText(next) });
    } catch (err) {
      setMessage({
        kind: "error",
        text:
          err instanceof ApiError
            ? err.message
            : "Could not save cloud credentials.",
      });
    } finally {
      setSaveBusy(false);
    }
  };

  const onRemoveCredentials = async () => {
    if (removeBusy) return;
    if (!confirmRemove) {
      setConfirmRemove(true);
      return;
    }
    setRemoveBusy(true);
    setMessage({ kind: "info", text: "Removing cloud credentials…" });
    try {
      const next = await api.deleteCloudCredentials();
      setCreds(next);
      setConfirmRemove(false);
      await refreshObservability();
      setMessage({ kind: "success", text: statusText(next) });
    } catch (err) {
      setMessage({
        kind: "error",
        text:
          err instanceof ApiError
            ? err.message
            : "Could not remove cloud credentials.",
      });
    } finally {
      setRemoveBusy(false);
    }
  };

  const canRemove =
    creds?.state === "configured" || creds?.state === "unreadable";

  return (
    <div class="container" data-page="cloud-archive" data-screen="cloud-archive">
      {/* ── Section 1: Sync status (idle) ── */}
      <div class="device-status-card device-status-present" id="syncStatusCard">
        <div class="device-status-header">
          <span class="status-dot status-present" />
          <div class="device-status-info">
            <strong>Cloud Sync</strong>
            <p data-testid="cloud-sync-subtitle">
              {uploaderSubtitle()} {queueSubtitle()}
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
            Configure provider credentials below to enable uploads.
          </span>
        </div>
      </div>

      {/* ── Sync statistics summary (honest pending) ── */}
      <div style="display: flex; gap: var(--space-4); flex-wrap: wrap; margin-bottom: var(--space-4);">
        {[
          [
            "Events Synced",
            uploadedFromHistory(history)?.toString() ?? "—",
          ],
          [
            "Events Pending",
            queueCount(queue, ["queued", "in_progress"])?.toString() ?? "—",
          ],
          ["Failed", queueCount(queue, ["failed"])?.toString() ?? "—"],
          ["Transferred", formatBytes(transferredFromHistory(history))],
        ].map(([label, value]) => (
          <div
            key={label}
            style="flex: 1; min-width: 120px; padding: var(--space-3); background: var(--bg-info); border-radius: var(--radius-md); text-align: center;"
          >
            <div style="font-size: var(--text-2xl); font-weight: 600; color: var(--text-primary);">
              {value}
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

      {/* ── Section 2: Cloud Provider ── */}
      <details class="settings-section" open>
        <summary>
          <Icon name="cloud" class="nav-icon" />
          Cloud Provider
        </summary>
        <div class="section-content">
          <p style="margin: 0 0 var(--space-3); color: var(--text-secondary);">
            Set up a cloud storage provider to automatically archive dashcam
            footage.
          </p>
          <div class="form-group" style="margin-bottom: var(--space-3);">
            <label for="providerSelect">
              <strong>Provider</strong>
            </label>
            <select
              id="providerSelect"
              class="settings-form-input"
              value={provider}
              disabled={saveBusy || removeBusy}
              onChange={(e) =>
                setProvider(
                  (e.currentTarget as HTMLSelectElement)
                    .value as CloudCredentialProvider,
                )
              }
              style="max-width: 360px;"
            >
              <option value="drive">Google Drive</option>
              <option value="onedrive">OneDrive</option>
              <option value="dropbox">Dropbox</option>
              <option value="s3" disabled>
                Amazon S3 (not yet supported)
              </option>
              <option value="b2" disabled>
                Backblaze B2 (not yet supported)
              </option>
              <option value="wasabi" disabled>
                Wasabi (not yet supported)
              </option>
              <option value="generic" disabled>
                NAS / Custom rclone (SFTP, WebDAV, SMB, FTP, ...) (not yet
                supported)
              </option>
            </select>
          </div>
          <div class="info-box" style="margin: 0 0 var(--space-3);">
            <p style="margin: 0 0 var(--space-2); color: var(--text-secondary);">
              On your PC, run:
            </p>
            <code>{`rclone authorize "${provider}"`}</code>
            <p style="margin: var(--space-2) 0 0; color: var(--text-secondary);">
              Paste the full output below, including the marker lines
              (&ldquo;Paste the following into your remote machine ---&gt;&rdquo;
              and &ldquo;&lt;---End paste&rdquo;). No manual edits needed.
            </p>
          </div>
          <div class="form-group" style="margin-bottom: var(--space-3);">
            <label for="cloudTokenInput">
              <strong>Authorization token output</strong>
            </label>
            <textarea
              id="cloudTokenInput"
              class="settings-form-input"
              value={token}
              disabled={saveBusy || removeBusy}
              onInput={(e) =>
                setToken((e.currentTarget as HTMLTextAreaElement).value)
              }
              rows={6}
              placeholder='Paste the full `rclone authorize` output here'
              style="width: 100%; min-height: 120px; resize: vertical;"
            />
          </div>
          <div
            id="cloudCredStatus"
            role="status"
            aria-live="polite"
            style={`font-size:0.85em;margin:0 0 var(--space-3);color:${
              message?.kind === "error"
                ? "var(--accent-error, #e53935)"
                : message?.kind === "success"
                  ? "var(--accent-success, #4caf50)"
                  : "var(--text-secondary)"
            }`}
          >
            {message?.text}
          </div>
          <div style="display:flex; gap: var(--space-2); align-items:center; flex-wrap: wrap;">
            <button
              type="button"
              id="cloudSaveBtn"
              class="edit-btn"
              disabled={saveBusy || removeBusy || token.trim().length === 0}
              onClick={onSaveCredentials}
              style="padding: 8px 16px; font-size: 14px;"
            >
              {saveBusy ? "Saving…" : "Save"}
            </button>
            {canRemove ? (
              confirmRemove ? (
                <>
                  <button
                    type="button"
                    id="cloudRemoveBtn"
                    class="edit-btn"
                    disabled={saveBusy || removeBusy}
                    onClick={onRemoveCredentials}
                    style="padding: 8px 16px; font-size: 14px; border-color: var(--accent-error, #e53935); color: var(--accent-error, #e53935);"
                  >
                    {removeBusy ? "Removing…" : "Confirm remove"}
                  </button>
                  <button
                    type="button"
                    class="edit-btn"
                    disabled={saveBusy || removeBusy}
                    onClick={() => setConfirmRemove(false)}
                    style="padding: 8px 16px; font-size: 14px;"
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  id="cloudRemoveBtn"
                  class="edit-btn"
                  disabled={saveBusy || removeBusy}
                  onClick={onRemoveCredentials}
                  style="padding: 8px 16px; font-size: 14px;"
                >
                  Remove
                </button>
              )
            ) : null}
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
          {queue && queue.items.length > 0 ? (
            <ul data-testid="cloud-queue-list" style="margin: 0; padding: 0; list-style: none; display: grid; gap: var(--space-2);">
              {queue.items.map((item: CloudQueueItem) => (
                <li
                  key={`${item.archive_item_id}:${item.child_key}:${item.seq}`}
                  style="padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-secondary);"
                >
                  <div style="display:flex; justify-content:space-between; gap: var(--space-2); flex-wrap: wrap;">
                    <strong style="font-size: var(--text-sm); color: var(--text-primary);">
                      {item.child_key} · {item.category}
                    </strong>
                    <span style="font-size: var(--text-xs); color: var(--text-muted); text-transform: capitalize;">
                      {item.state.replace("_", " ")}
                    </span>
                  </div>
                  <div style="font-size: var(--text-xs); color: var(--text-secondary); margin-top: 4px;">
                    {formatBytes(item.bytes_uploaded)} / {formatBytes(item.total_bytes)} • attempts {item.attempts}
                  </div>
                  {item.last_error_class ? (
                    <div style="font-size: var(--text-xs); color: var(--accent-error, #e53935); margin-top: 4px;">
                      Error class: {item.last_error_class}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
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
                {queueError
                  ? "Cloud queue is temporarily unavailable."
                  : "No uploads are queued right now."}
              </p>
            </div>
          )}
        </div>
      </details>

      {/* ── Section 4: Sync History (empty) ── */}
      <details class="settings-section">
        <summary>
          <Icon name="bar-chart-2" class="nav-icon" />
          Sync History
        </summary>
        <div class="section-content">
          {history && history.items.length > 0 ? (
            <ul data-testid="cloud-history-list" style="margin: 0; padding: 0; list-style: none; display: grid; gap: var(--space-2);">
              {history.items.map((item) => (
                <li
                  key={`${item.id}:${item.completion_seq}`}
                  style="padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-secondary);"
                >
                  <div style="display:flex; justify-content:space-between; gap: var(--space-2); flex-wrap: wrap;">
                    <strong style="font-size: var(--text-sm); color: var(--text-primary);">
                      {item.child_key}
                    </strong>
                    <span style="font-size: var(--text-xs); color: var(--text-muted); text-transform: capitalize;">
                      {item.outcome}
                    </span>
                  </div>
                  <div style="font-size: var(--text-xs); color: var(--text-secondary); margin-top: 4px;">
                    {formatBytes(item.size_bytes)} • {formatSavedAt(item.at)}
                  </div>
                  {item.error_class ? (
                    <div style="font-size: var(--text-xs); color: var(--accent-error, #e53935); margin-top: 4px;">
                      {item.error_class}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
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
              <p style="margin: 0;">
                {historyError ? "Cloud history is temporarily unavailable." : "No sync sessions yet"}
              </p>
            </div>
          )}
        </div>
      </details>
    </div>
  );
}
