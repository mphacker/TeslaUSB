import { Icon } from "./Icon";
import { useFileDrop } from "../hooks/useFileDrop";
import { fmtBytes } from "../hooks/useMediaCategory";
import type { UseMediaCategory, UploadItem } from "../hooks/useMediaCategory";
import "../styles/media-upload.css";

interface MediaUploadZoneProps {
  cat: UseMediaCategory;
  /** `data-testid` for the drop zone (e.g. `"wraps-dropzone"`). */
  testId: string;
  /** Native `accept` attribute for the file picker. */
  accept: string;
  /** Icon name shown above the title. */
  icon: string;
  title: string;
  hint: string;
}

function statusGlyph(status: UploadItem["status"]): string {
  switch (status) {
    case "uploading":
      return "↻";
    case "done":
      return "✓";
    case "error":
      return "✗";
    default:
      return "•";
  }
}

/**
 * Shared drag-and-drop upload zone for the media category screens. Supports
 * multiple files, shows per-file upload status + an overall progress counter,
 * and (via `useMediaCategory`) refetches the list after each file so uploads
 * appear in the library immediately without a page refresh.
 */
export function MediaUploadZone({
  cat,
  testId,
  accept,
  icon,
  title,
  hint,
}: MediaUploadZoneProps) {
  const drop = useFileDrop(cat.onFilesDropped, { disabled: cat.uploading });
  const showStatus = cat.uploadItems.length > 0;
  const rows: UploadItem[] = showStatus
    ? cat.uploadItems
    : cat.selectedFiles.map((f) => ({
        name: f.name,
        size: f.size,
        status: "pending" as const,
      }));

  return (
    <form
      class={`media-upload-zone${drop.dragging ? " dragging" : ""}`}
      onSubmit={cat.onUploadSubmit}
      aria-busy={cat.uploading}
      data-testid={testId}
      {...drop.dropHandlers}
    >
      <Icon name={icon} class="media-upload-icon" />
      <p class="media-upload-title">{title}</p>
      <p class="media-upload-hint">{hint}</p>
      <input
        ref={cat.fileInputRef}
        type="file"
        accept={accept}
        multiple
        onChange={cat.onFileChange}
        disabled={cat.uploading}
        aria-label={title}
      />

      {cat.uploadProgress && (
        <p class="media-upload-progress" role="status">
          Uploading {cat.uploadProgress.current}/{cat.uploadProgress.total}…
        </p>
      )}

      {rows.length > 0 && (
        <ul class="media-upload-list">
          {rows.map((it) => (
            <li key={it.name} class={`media-upload-row status-${it.status}`}>
              <span class="muf-status" aria-hidden="true">
                {statusGlyph(it.status)}
              </span>
              <span class="muf-name">{it.name}</span>
              <span class="muf-size">{fmtBytes(it.size)}</span>
              {!cat.uploading && !showStatus && (
                <button
                  type="button"
                  class="muf-remove"
                  aria-label={`Remove ${it.name}`}
                  onClick={() => cat.removeStagedFile(it.name)}
                >
                  ×
                </button>
              )}
              {it.status === "error" && it.error && (
                <span class="muf-error">{it.error}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {cat.uploadFail && (
        <p role="alert" class="media-upload-error">
          {cat.uploadFail.message}
          {cat.uploadFail.retryable && (
            <>
              {" "}
              <button
                type="submit"
                class="action-btn"
                disabled={cat.selectedFiles.length === 0}
              >
                Retry
              </button>
            </>
          )}
        </p>
      )}

      <button
        type="submit"
        class="action-btn media-upload-submit"
        disabled={cat.selectedFiles.length === 0 || cat.uploading}
        aria-busy={cat.uploading}
      >
        {cat.uploading
          ? "Uploading…"
          : cat.selectedFiles.length > 1
            ? `Upload ${cat.selectedFiles.length} files`
            : "Upload"}
      </button>
    </form>
  );
}
