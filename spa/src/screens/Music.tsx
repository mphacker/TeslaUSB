import { useEffect, useState } from "preact/hooks";
import { Icon } from "../components/Icon";
import { MediaPills } from "../components/MediaPills";
import { useScreenHook } from "../components/screenHook";
import { api } from "../api/client";
import { fmtBytes } from "../hooks/useMediaCategory";
import { useMusicLibrary } from "../hooks/useMusicLibrary";
import type { StorageInfo } from "../api/types";
import "../styles/music.css";

/**
 * Music screen (route `/music`) — v1 look-and-feel parity.
 *
 * Uses the Music-specific `useMusicLibrary` hook instead of the shared
 * `useMediaCategory` so the other four media screens are untouched.
 * Features: folder navigation, upload into subfolder, create/delete folders,
 * full <audio controls> player, poll-until-convergence auto-refresh for all
 * mutations (mirrors ChimeScheduler's 2 s / 45 s bounded pattern).
 */
export function Music() {
  useScreenHook("music");
  const lib = useMusicLibrary();

  // Storage info for the usage bar / stat tiles
  const [storageInfo, setStorageInfo] = useState<StorageInfo | null>(null);
  const [dragging, setDragging] = useState(false);
  useEffect(() => {
    const ctrl = new AbortController();
    api
      .storage(ctrl.signal)
      .then(setStorageInfo)
      .catch(() => {});
    return () => ctrl.abort();
  }, []);

  // The Music screen opts out of the global 1200px `.main-content` cap (the v1
  // mock was authored at 1440px) so the files table isn't crunched inside the
  // two-column layout. Mirror TripMap's body-class pattern, scoped to while this
  // screen is mounted.
  useEffect(() => {
    document.body.classList.add("music-active");
    return () => document.body.classList.remove("music-active");
  }, []);

  // Local state for the right-panel inputs
  const [folderInput, setFolderInput] = useState("");
  // Move-dialog destination (folder path relative under Music/, "" = root)
  const [moveDest, setMoveDest] = useState("");
  const [moveNewName, setMoveNewName] = useState("");

  // ── Storage stats ──────────────────────────────────────────────────────────
  const fs = storageInfo?.filesystems[0] ?? null;
  const usedBytes = fs !== null ? fs.total_bytes - fs.free_bytes : null;
  const freeBytes = fs?.free_bytes ?? null;
  const totalBytes = fs?.total_bytes ?? null;
  const usagePct =
    totalBytes && usedBytes !== null
      ? Math.min(100, (usedBytes / totalBytes) * 100)
      : 0;

  // ── Optimistic-UI helpers ──────────────────────────────────────────────────
  // All paths below are full rel_paths (include "Music/" prefix).

  const currentPrefix =
    lib.currentPath.length === 0
      ? "Music/"
      : `Music/${lib.currentPath.join("/")}/`;

  // Uploads pending in the current folder
  const pendingUploads = lib.pendingOps.filter(
    (op) =>
      op.kind === "upload" &&
      op.targetPath.startsWith(currentPrefix) &&
      !op.targetPath.slice(currentPrefix.length).includes("/"),
  );

  // Folder-creates pending directly under current folder
  const pendingFolderCreates = lib.pendingOps.filter(
    (op) =>
      op.kind === "createFolder" &&
      op.targetPath.startsWith(currentPrefix) &&
      !op.targetPath.slice(currentPrefix.length).includes("/"),
  );

  // Folders being deleted (keyed by label = folder name)
  const pendingDeleteFolders = new Set(
    lib.pendingOps
      .filter((op) => op.kind === "deleteFolder")
      .map((op) => op.label),
  );

  // File rel_paths currently being deleted
  const pendingDeletePaths = new Set(
    lib.pendingOps
      .filter((op) => op.kind === "deleteFiles")
      .flatMap((op) => op.filePaths ?? []),
  );

  // File rel_paths currently being moved (source side)
  const pendingMovePaths = new Set(
    lib.pendingOps
      .filter((op) => op.kind === "move")
      .map((op) => op.moveFrom ?? ""),
  );

  // ── Keyboard handler for folder rows ──────────────────────────────────────
  function handleFolderKeyDown(e: KeyboardEvent, name: string) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!pendingDeleteFolders.has(name)) lib.enterFolder(name);
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div class="container media-page" data-page="music" data-screen="music">
      <MediaPills active="music" />
      <h2>Music Library</h2>

      <div class="music-info-box" data-testid="music-info-banner">
        <p>
          Tesla only scans music inside the <code>/Music</code> folder. This
          page always reads and uploads inside that folder; paths shown below
          are relative to <code>/Music</code>.
        </p>
      </div>

      {lib.notice && (
        <div
          class="settings-section"
          role="status"
          style="color: var(--accent-success);"
        >
          {lib.notice}{" "}
          <button
            class="action-btn"
            style="font-size:12px;padding:2px 8px;"
            onClick={lib.clearNotice}
          >
            Dismiss
          </button>
        </div>
      )}

      <div id="music-page" class="music-layout" data-testid="music-layout">
        {/* ── Library / browser panel ── */}
        <div class="music-panel" data-testid="music-library-panel">
          {/* Storage summary */}
          <div class="music-summary" data-testid="music-stats">
            <div class="stat-item">
              <div class="stat-label">Used</div>
              <div class="stat-value">{fmtBytes(usedBytes)}</div>
            </div>
            <div class="stat-item">
              <div class="stat-label">Free</div>
              <div class="stat-value">{fmtBytes(freeBytes)}</div>
            </div>
            <div class="stat-item">
              <div class="stat-label">Files</div>
              <div class="stat-value">
                {lib.state.tag === "ready" ? lib.totalFileCount : "—"}
              </div>
            </div>
          </div>

          <div class="music-meter" aria-label="Usage">
            <span style={`width: ${usagePct.toFixed(2)}%`} />
          </div>

          {/* Breadcrumb */}
          <div class="music-breadcrumb" data-testid="music-breadcrumb">
            <span>
              {"📂 "}
              <button
                class="breadcrumb-link"
                onClick={() => lib.navigateTo(-1)}
                aria-label="Navigate to Music root"
              >
                /Music
              </button>
              {lib.currentPath.map((seg, i) => (
                <span key={`bc-${i}`}>
                  {" / "}
                  {i < lib.currentPath.length - 1 ? (
                    <button
                      class="breadcrumb-link"
                      onClick={() => lib.navigateTo(i)}
                      aria-label={`Navigate to ${seg}`}
                    >
                      {seg}
                    </button>
                  ) : (
                    <span aria-current="location">{seg}</span>
                  )}
                </span>
              ))}
            </span>
          </div>

          {/* Waiting / Refresh now */}
          {lib.hasWaiting && (
            <div class="settings-section music-waiting-notice" role="status">
              Sync is taking longer than expected.{" "}
              <button class="action-btn" onClick={lib.refetch}>
                Refresh now
              </button>
            </div>
          )}

          {/* Load states */}
          {lib.state.tag === "loading" && (
            <div role="status" aria-busy="true" data-testid="music-loading">
              Loading…
            </div>
          )}
          {lib.state.tag === "error" && (
            <div role="alert" data-testid="music-error">
              Couldn't load music library.{" "}
              <button class="action-btn" onClick={lib.refetch}>
                Retry
              </button>
            </div>
          )}

          {lib.state.tag === "ready" && (
            <>
              {/* ── Folders table (desktop) ── */}
              {(lib.subfolders.length > 0 ||
                pendingFolderCreates.length > 0) && (
                <>
                  <div class="video-table-container">
                    <table
                      class="settings-table"
                      data-testid="music-folder-list"
                      style="table-layout: fixed;"
                    >
                      <thead>
                        <tr>
                          <th>Folders</th>
                          <th style="width: 100px; text-align: right;">
                            Actions
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {lib.subfolders.map((folder) => {
                          const removing = pendingDeleteFolders.has(folder);
                          return (
                            <tr
                              key={folder}
                              class={`music-folder-row${removing ? " music-pending-row" : ""}`}
                              data-testid={`music-folder-row-${folder}`}
                              tabIndex={removing ? -1 : 0}
                              role="link"
                              onClick={() =>
                                !removing && lib.enterFolder(folder)
                              }
                              onKeyDown={(e) =>
                                handleFolderKeyDown(
                                  e as unknown as KeyboardEvent,
                                  folder,
                                )
                              }
                              aria-label={`Enter folder ${folder}`}
                            >
                              <td style="color: var(--text-link);">
                                {"📁 "}
                                {folder}
                                {removing && (
                                  <span class="music-op-status">
                                    {" "}— Removing…
                                  </span>
                                )}
                              </td>
                              <td style="text-align: right;">
                                <button
                                  class="action-btn danger"
                                  data-testid={`music-delete-folder-${folder}`}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    lib.onDeleteFolder(folder);
                                  }}
                                  disabled={removing}
                                  aria-label={`Delete folder ${folder}`}
                                >
                                  Delete
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                        {/* Ghost rows for pending creates */}
                        {pendingFolderCreates.map((op) => (
                          <tr
                            key={`pf-${op.token}`}
                            class="music-folder-row music-pending-row"
                          >
                            <td style="color: var(--text-secondary);">
                              {"📁 "}
                              {op.label}
                              <span class="music-op-status">
                                {" "}—{" "}
                                {op.phase === "syncing"
                                  ? "Creating…"
                                  : "Syncing…"}
                              </span>
                            </td>
                            <td />
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  {/* ── Mobile folder cards ── */}
                  <div
                    class="music-mobile-folders"
                    data-testid="music-mobile-folders"
                  >
                    {lib.subfolders.map((folder) => {
                      const removing = pendingDeleteFolders.has(folder);
                      return (
                        <div
                          key={folder}
                          class="music-mobile-folder"
                          data-testid={`music-mobile-folder-row-${folder}`}
                          tabIndex={removing ? -1 : 0}
                          role="link"
                          onClick={() =>
                            !removing && lib.enterFolder(folder)
                          }
                          onKeyDown={(e) =>
                            handleFolderKeyDown(
                              e as unknown as KeyboardEvent,
                              folder,
                            )
                          }
                          aria-label={`Enter folder ${folder}`}
                        >
                          <span class="music-mobile-folder-name">
                            {"📁 "}
                            {folder}
                          </span>
                          <span class="music-mobile-folder-actions">
                            <button
                              class="action-btn danger"
                              data-testid={`music-mobile-delete-folder-${folder}`}
                              onClick={(e) => {
                                e.stopPropagation();
                                lib.onDeleteFolder(folder);
                              }}
                              disabled={removing}
                              aria-label={`Delete folder ${folder}`}
                            >
                              Delete
                            </button>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}

              {/* ── Files section ── */}
              <h3 style="margin-top: 10px;">Files</h3>

              {/* Bulk-delete bar */}
              {lib.selected.size > 0 && (
                <div class="settings-section music-bulk-bar">
                  <button
                    class="action-btn danger"
                    onClick={() => lib.onDeleteFiles([...lib.selected])}
                    disabled={lib.deletingFiles}
                    aria-label={`Delete ${lib.selected.size} selected files`}
                  >
                    {lib.deletingFiles
                      ? "Deleting…"
                      : `Delete ${lib.selected.size} file${lib.selected.size === 1 ? "" : "s"}`}
                  </button>{" "}
                  <button
                    class="action-btn"
                    onClick={lib.clearSelection}
                    disabled={lib.deletingFiles}
                  >
                    Clear selection
                  </button>
                  {lib.deleteFilesFail && (
                    <p
                      role="alert"
                      style="color: var(--accent-error); margin: 6px 0 0;"
                    >
                      {lib.deleteFilesFail.message}
                    </p>
                  )}
                </div>
              )}

              {/* Global empty state */}
              {lib.currentFiles.length === 0 &&
                pendingUploads.length === 0 &&
                lib.allItems.length === 0 && (
                  <div
                    class="music-empty"
                    data-testid="music-empty"
                    role="status"
                  >
                    <Icon name="music" class="empty-icon" />
                    <p>No music files installed yet.</p>
                  </div>
                )}

              {/* Empty-folder state (library has files but none here) */}
              {lib.currentFiles.length === 0 &&
                pendingUploads.length === 0 &&
                lib.allItems.length > 0 && (
                  <div
                    class="music-empty"
                    data-testid="music-empty-folder"
                    role="status"
                  >
                    <Icon name="music" class="empty-icon" />
                    <p>No files in this folder.</p>
                  </div>
                )}

              {/* Files table (desktop) */}
              {(lib.currentFiles.length > 0 || pendingUploads.length > 0) && (
                <>
                  <div class="video-table-container">
                    <table
                      class="settings-table media-card-table"
                      data-testid="music-list"
                      style="table-layout: fixed;"
                    >
                      <thead>
                        <tr>
                          <th
                            class="bulk-check-col"
                            aria-label="Select"
                            style="width: 36px;"
                          />
                          <th>Name</th>
                          <th style="width: 80px;">Size</th>
                          <th style="width: 320px;">Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {lib.currentFiles.map((item) => {
                          const removing = pendingDeletePaths.has(
                            item.rel_path,
                          );
                          const moving = pendingMovePaths.has(item.rel_path);
                          const busy = removing || moving;
                          const checked = lib.selected.has(item.rel_path);
                          return (
                            <tr
                              key={item.rel_path}
                              class={
                                checked ? "media-row-selected" : undefined
                              }
                            >
                              <td class="bulk-check-col">
                                <input
                                  type="checkbox"
                                  class="bulk-row-check"
                                  checked={checked}
                                  onChange={() =>
                                    lib.toggleSelect(item.rel_path)
                                  }
                                  disabled={busy || lib.deletingFiles}
                                  aria-label={`Select ${item.name}`}
                                />
                              </td>
                              <td class="media-card-title" style="word-break: break-all;">
                                {item.name}
                                {removing && (
                                  <span class="music-op-status">
                                    {" "}— Removing…
                                  </span>
                                )}
                                {moving && (
                                  <span class="music-op-status">
                                    {" "}— Moving…
                                  </span>
                                )}
                              </td>
                              <td data-label="Size">{fmtBytes(item.size_bytes)}</td>
                              <td class="media-card-actions">
                                <audio
                                  class="media-row-player"
                                  controls
                                  preload="none"
                                  data-testid="music-audio"
                                  src={api.mediaContentUrl(
                                    item.rel_path,
                                    item.modified,
                                  )}
                                  aria-label={`Play ${item.name}`}
                                />
                                <div class="music-file-btn-row">
                                  <button
                                    class="action-btn"
                                    onClick={() =>
                                      lib.openMoveDialog(item.rel_path)
                                    }
                                    disabled={busy}
                                    aria-label={`Move ${item.name}`}
                                    data-testid={`music-move-${item.name}`}
                                  >
                                    Move
                                  </button>
                                  <button
                                    class="action-btn danger"
                                    onClick={() =>
                                      lib.onDeleteFiles([item.rel_path])
                                    }
                                    disabled={busy || lib.deletingFiles}
                                    aria-label={`Delete ${item.name}`}
                                    data-testid={`music-delete-${item.name}`}
                                  >
                                    Delete
                                  </button>
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                        {/* Ghost rows for pending uploads */}
                        {pendingUploads.map((op) => (
                          <tr
                            key={`pu-${op.token}`}
                            class="music-pending-row"
                          >
                            <td />
                            <td
                              style="color: var(--text-secondary);"
                              colSpan={3}
                            >
                              {op.label}
                              <span class="music-op-status">
                                {" "}—{" "}
                                {op.phase === "syncing"
                                  ? "Uploading…"
                                  : "Syncing…"}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  {/* ── Mobile file cards ── */}
                  <div
                    class="music-mobile-files"
                    data-testid="music-mobile-files"
                  >
                    {lib.currentFiles.map((item) => {
                      const removing = pendingDeletePaths.has(item.rel_path);
                      const moving = pendingMovePaths.has(item.rel_path);
                      const busy = removing || moving;
                      return (
                        <div
                          key={item.rel_path}
                          class="music-mobile-file"
                        >
                          <div class="music-mobile-file-name">
                            {"🎵 "}
                            {item.name}
                            {removing && (
                              <span class="music-op-status">
                                {" "}— Removing…
                              </span>
                            )}
                            {moving && (
                              <span class="music-op-status">
                                {" "}— Moving…
                              </span>
                            )}
                          </div>
                          <div class="music-mobile-file-size">
                            {fmtBytes(item.size_bytes)}
                          </div>
                          <audio
                            controls
                            preload="none"
                            src={api.mediaContentUrl(
                              item.rel_path,
                              item.modified,
                            )}
                            aria-label={`Play ${item.name}`}
                          />
                          <div class="music-mobile-file-actions">
                            <button
                              class="action-btn"
                              data-testid={`music-mobile-move-${item.name}`}
                              onClick={() =>
                                lib.openMoveDialog(item.rel_path)
                              }
                              disabled={busy}
                              aria-label={`Move ${item.name}`}
                            >
                              Move
                            </button>
                            <button
                              class="action-btn danger"
                              data-testid={`music-mobile-delete-${item.name}`}
                              onClick={() =>
                                lib.onDeleteFiles([item.rel_path])
                              }
                              disabled={busy || lib.deletingFiles}
                              aria-label={`Delete ${item.name}`}
                            >
                              Delete
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
            </>
          )}
        </div>

        {/* ── Upload / create panel ── */}
        <div class="music-panel" data-testid="music-upload-panel">
          <h3 class="music-panel-title">Upload</h3>
          <p class="music-upload-intro">
            Target folder:{" "}
            <strong>
              {lib.currentPath.length === 0
                ? "/"
                : `/${lib.currentPath.join("/")}`}
            </strong>
            . Uploading music momentarily ejects the USB drive.
          </p>

          <div
            class={`music-drop-zone${dragging ? " dragging" : ""}`}
            data-testid="music-dropzone"
            onClick={() => {
              if (!lib.uploading) lib.fileInputRef.current?.click();
            }}
            onDragOver={(e) => {
              e.preventDefault();
              if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
            }}
            onDragEnter={(e) => {
              e.preventDefault();
              if (!lib.uploading) setDragging(true);
            }}
            onDragLeave={(e) => {
              e.preventDefault();
              // Only clear when leaving the zone itself, not a child element.
              if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
                setDragging(false);
              }
            }}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              if (lib.uploading) return;
              const files = Array.from(e.dataTransfer?.files ?? []);
              if (files.length > 0) lib.onFilesDropped(files);
            }}
          >
            <Icon name="music" class="drop-icon" />
            <div class="drop-label">Drop files here or click to choose</div>
            <div class="drop-hint">
              Allowed: mp3, flac, wav, aac, m4a. Max 10 MB each.
            </div>
            <input
              ref={lib.fileInputRef}
              type="file"
              accept=".mp3,.flac,.wav,.aac,.m4a"
              multiple
              onChange={lib.onFileChange}
              onClick={(e) => e.stopPropagation()}
              disabled={lib.uploading}
              aria-label="Choose music files"
            />
            {lib.selectedFiles.length > 0 && (
              <ul class="drop-selected" data-testid="music-selected-files">
                {lib.selectedFiles.map((f) => (
                  <li key={`${f.name}:${f.size}`}>
                    {f.name} ({fmtBytes(f.size)})
                  </li>
                ))}
              </ul>
            )}
          </div>

          {lib.uploadFail && (
            <p role="alert" style="color: var(--accent-error); margin: 8px 0;">
              {lib.uploadFail.message}
            </p>
          )}

          <div class="music-upload-actions" data-testid="music-upload-actions">
            <button
              type="button"
              class="action-btn"
              disabled={lib.selectedFiles.length === 0 || lib.uploading}
              onClick={lib.onUploadSubmit}
              aria-busy={lib.uploading}
              data-testid="music-upload-btn"
            >
              {lib.uploading
                ? lib.uploadProgress
                  ? `Uploading ${lib.uploadProgress.done}/${lib.uploadProgress.total}…`
                  : "Uploading…"
                : lib.selectedFiles.length > 1
                  ? `Upload ${lib.selectedFiles.length} files`
                  : "Upload"}
            </button>
            <button
              type="button"
              class="action-btn danger"
              disabled={lib.selectedFiles.length === 0 || lib.uploading}
              onClick={lib.clearSelectedFile}
              data-testid="music-clear-btn"
            >
              Clear
            </button>
          </div>

          {/* Create-folder row */}
          <div
            class="music-folder-input"
            data-testid="music-folder-input"
          >
            <input
              type="text"
              placeholder="New folder name"
              value={folderInput}
              onInput={(e) =>
                setFolderInput((e.target as HTMLInputElement).value)
              }
              aria-label="New folder name"
              data-testid="music-folder-name-input"
            />
            <button
              class="action-btn"
              onClick={() => {
                lib.onCreateFolder(folderInput);
                setFolderInput("");
              }}
              disabled={!folderInput.trim() || lib.createFolderBusy}
              aria-label="Create folder"
              data-testid="music-create-folder-btn"
            >
              {lib.createFolderBusy ? "Creating…" : "Create Folder"}
            </button>
          </div>

          {lib.createFolderFail && (
            <p role="alert" style="color: var(--accent-error); margin: 6px 0;">
              {lib.createFolderFail.message}
            </p>
          )}

          <div class="music-status" data-testid="music-status" />
        </div>
      </div>

      {/* ── Move-file dialog ── */}
      {lib.moveDialogRelPath && (
        <div class="settings-section" role="dialog" aria-label="Move file">
          <p>
            Move{" "}
            <strong>
              {lib.moveDialogRelPath.split("/").pop()}
            </strong>{" "}
            to:
          </p>
          <select
            value={moveDest}
            onChange={(e) =>
              setMoveDest((e.target as HTMLSelectElement).value)
            }
            aria-label="Destination folder"
            data-testid="music-move-dest"
          >
            <option value="">/ (root)</option>
            {lib.availableFolders
              .filter((f) => f !== "")
              .map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
          </select>
          <input
            type="text"
            value={moveNewName}
            onInput={(e) =>
              setMoveNewName((e.target as HTMLInputElement).value)
            }
            aria-label="New filename (optional)"
            placeholder="Keep original name"
            data-testid="music-move-newname"
          />
          {lib.moveFail && (
            <p role="alert" style="color: var(--accent-error);">
              {lib.moveFail.message}
            </p>
          )}
          <div style="margin-top: 8px;">
            <button
              class="action-btn"
              onClick={() => {
                lib.onConfirmMove(moveDest, moveNewName);
                setMoveDest("");
                setMoveNewName("");
              }}
              disabled={lib.moveBusy}
              aria-busy={lib.moveBusy}
              data-testid="music-move-confirm-btn"
            >
              {lib.moveBusy ? "Moving…" : "Move"}
            </button>{" "}
            <button
              class="action-btn"
              onClick={() => {
                setMoveDest("");
                setMoveNewName("");
                lib.closeMoveDialog();
              }}
              disabled={lib.moveBusy}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
