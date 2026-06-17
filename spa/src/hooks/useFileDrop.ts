import { useState } from "preact/hooks";

export interface FileDrop {
  /** True while a drag is hovering the zone — bind to a `.dragging` class. */
  dragging: boolean;
  /** Spread onto the drop-zone element to enable drag-and-drop. */
  dropHandlers: {
    onDragOver: (e: DragEvent) => void;
    onDragEnter: (e: DragEvent) => void;
    onDragLeave: (e: DragEvent) => void;
    onDrop: (e: DragEvent) => void;
  };
}

/**
 * Drag-and-drop file support for an upload zone. Manages the drag-active visual
 * state and calls `onFiles` with the dropped `File`s. `preventDefault` on
 * dragover/drop is what actually lets the browser drop onto the element.
 */
export function useFileDrop(
  onFiles: (files: File[]) => void,
  opts?: { disabled?: boolean },
): FileDrop {
  const [dragging, setDragging] = useState(false);
  const disabled = opts?.disabled ?? false;
  return {
    dragging,
    dropHandlers: {
      onDragOver: (e) => {
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      },
      onDragEnter: (e) => {
        e.preventDefault();
        if (!disabled) setDragging(true);
      },
      onDragLeave: (e) => {
        e.preventDefault();
        // Only clear when the pointer leaves the zone itself, not a child.
        const el = e.currentTarget as Element;
        if (!el.contains(e.relatedTarget as Node | null)) setDragging(false);
      },
      onDrop: (e) => {
        e.preventDefault();
        setDragging(false);
        if (disabled) return;
        const files = Array.from(e.dataTransfer?.files ?? []);
        if (files.length > 0) onFiles(files);
      },
    },
  };
}
