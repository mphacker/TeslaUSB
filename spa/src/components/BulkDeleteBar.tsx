import { Icon } from "./Icon";
import type { UseMediaCategory } from "../hooks/useMediaCategory";

/**
 * Shared bulk-select toolbar + confirm dialog for the toybox media screens.
 *
 * Driven entirely by the {@link UseMediaCategory} hook state, so every category
 * that opts into bulk delete (by passing a `bulkDelete` to the hook) gets an
 * identical, single-handoff "Delete selected" affordance. The per-row
 * checkboxes live inside each screen's own table; this component renders the
 * toolbar above the table and the confirm dialog.
 *
 * Renders nothing when bulk delete is not enabled or the list is empty.
 */
export function BulkDeleteBar({
  cat,
  noun,
}: {
  cat: UseMediaCategory;
  /** Plural noun for the count label, e.g. "sounds", "tracks", "images". */
  noun: string;
}) {
  if (!cat.bulkEnabled || cat.state.tag !== "ready") return null;
  const total = cat.state.items.length;
  if (total === 0) return null;

  const count = cat.selected.size;
  const allSelected = count === total && total > 0;

  return (
    <div class="bulk-delete-bar" data-testid="bulk-bar">
      <div class="bulk-delete-toolbar">
        <label class="bulk-select-all">
          <input
            type="checkbox"
            checked={allSelected}
            // `indeterminate` is a DOM-only property (no HTML attribute); set it
            // via a ref callback so a partial selection shows the dash state.
            ref={(el) => {
              if (el) el.indeterminate = count > 0 && !allSelected;
            }}
            onChange={() => (allSelected ? cat.clearSelection() : cat.selectAll())}
            aria-label={allSelected ? "Deselect all" : "Select all"}
            disabled={cat.bulkDeleting}
          />
          <span>
            {count > 0 ? `${count} selected` : `Select all ${total} ${noun}`}
          </span>
        </label>
        <button
          class="action-btn danger"
          data-testid="bulk-delete-btn"
          onClick={cat.onRequestBulkDelete}
          disabled={count === 0 || cat.bulkDeleting}
          aria-label={`Delete ${count} selected ${noun}`}
        >
          <Icon name="trash-2" style="width: 14px; height: 14px;" />{" "}
          Delete selected{count > 0 ? ` (${count})` : ""}
        </button>
      </div>

      {cat.confirmBulk && (
        <div
          class="settings-section"
          role="dialog"
          aria-label="Confirm bulk remove"
          data-testid="bulk-confirm"
        >
          <p>
            Remove <strong>{count}</strong> {noun}? This ejects the USB drive
            momentarily — all {count} are removed in one operation.
          </p>
          {cat.bulkFail && (
            <p role="alert" style="color: var(--accent-error);">
              {cat.bulkFail.message}
            </p>
          )}
          <button
            class="action-btn danger"
            onClick={cat.onConfirmBulkDelete}
            disabled={cat.bulkDeleting}
            aria-busy={cat.bulkDeleting}
            data-testid="bulk-confirm-btn"
          >
            {cat.bulkDeleting ? "Removing…" : `Remove ${count}`}
          </button>{" "}
          <button
            class="action-btn"
            onClick={cat.onCancelBulkDelete}
            disabled={cat.bulkDeleting}
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
