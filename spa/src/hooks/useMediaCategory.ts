/**
 * Shared hook for toybox media category screens (Boombox, Music, LightShows,
 * LicensePlates, Wraps). Encapsulates the load/install/remove lifecycle so
 * each screen only declares its API calls and renders the already-typed state.
 *
 * Design mirrors the chimes pattern in `Media.tsx`:
 *  - GET on mount with AbortController cleanup.
 *  - Install: busy → handoff → refresh.
 *  - Remove: confirm → busy → handoff → refresh.
 *  - Failure classifier: retryable (network/409/503) vs terminal (4xx/502/500).
 */

import { useEffect, useRef, useState } from "preact/hooks";
import type { RefObject } from "preact";
import { ApiError, isQueued } from "../api/client";
import { subscribeMediaEvents } from "../api/mediaEvents";
import type { MediaItem, MediaList } from "../api/types";

export type { MediaItem };

export type LoadState =
  | { tag: "loading" }
  | { tag: "error" }
  | { tag: "ready"; items: MediaItem[] };

export interface MediaFailure {
  message: string;
  retryable: boolean;
}

/** Per-file row state shown in the upload zone during a multi-file upload. */
export interface UploadItem {
  name: string;
  size: number;
  status: "pending" | "uploading" | "done" | "error";
  error?: string;
}

/** Map an install/remove rejection to operator-facing UI state. */
export function classifyMediaFailure(err: unknown): MediaFailure {
  if (err instanceof ApiError) {
    if (err.status === 0 || err.code === "network") {
      return {
        message: "Couldn't reach the device. Check the connection and try again.",
        retryable: true,
      };
    }
    if (err.status === 409) {
      const base = err.message || "The vehicle is busy saving a clip right now.";
      return { message: `${base} You can retry in a moment.`, retryable: true };
    }
    if (err.status === 503) {
      return {
        message: "The device service is unavailable. Try again once it's back.",
        retryable: true,
      };
    }
    if (err.status === 413 || err.code === "file_too_large") {
      return {
        message: "That file is too large to upload. Choose a smaller file and try again.",
        retryable: false,
      };
    }
    if (err.status === 400 || err.status === 422) {
      return { message: err.message, retryable: false };
    }
    if (err.status === 502) {
      return {
        message: `The change couldn't be completed on the car: ${err.message}`,
        retryable: false,
      };
    }
    if (err.status === 500) {
      return {
        message: `The device reported a fault: ${err.message}`,
        retryable: false,
      };
    }
    return { message: err.message, retryable: false };
  }
  return {
    message: (err as Error).message || "Unexpected error.",
    retryable: true,
  };
}

/** Format a byte count for display. */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return "—";
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

interface UseMediaCategoryOptions {
  /** `GET` function: returns `Promise<MediaList>`. */
  fetchList: (signal?: AbortSignal) => Promise<MediaList>;
  /** `POST` install function. Resolves with the mutation result (`state`). */
  install: (
    file: File | Blob,
    signal?: AbortSignal,
  ) => Promise<{ state?: string }>;
  /** `DELETE` remove function. Resolves with the mutation result (`state`). */
  remove: (name: string, signal?: AbortSignal) => Promise<{ state?: string }>;
  /**
   * `POST /bulk-delete` function. Optional: a category that supplies it gets
   * the multi-select + "Delete selected" affordances; one without it keeps
   * single-row remove only.
   */
  bulkDelete?: (
    names: string[],
    signal?: AbortSignal,
  ) => Promise<{ state?: string }>;
  /**
   * Lowercase file extensions (e.g. `[".png"]`) accepted by this category.
   * Dropped files are filtered against this client-side (the native input's
   * `accept` attribute only governs the picker, not drag-and-drop). Omit to
   * accept anything the server will validate.
   */
  accept?: string[];
}

export interface UseMediaCategory {
  state: LoadState;
  // Upload
  fileInputRef: RefObject<HTMLInputElement>;
  selectedFiles: File[];
  uploading: boolean;
  uploadProgress: { current: number; total: number } | null;
  uploadItems: UploadItem[];
  uploadFail: MediaFailure | null;
  notice: string | null;
  // Remove
  confirmRemoveName: string | null;
  removing: boolean;
  removeFail: MediaFailure | null;
  // Bulk select / delete (only meaningful when `bulkDelete` was supplied)
  bulkEnabled: boolean;
  selected: ReadonlySet<string>;
  confirmBulk: boolean;
  bulkDeleting: boolean;
  bulkFail: MediaFailure | null;
  // Handlers
  onFileChange: (e: Event) => void;
  onFilesDropped: (files: File[]) => void;
  removeStagedFile: (name: string) => void;
  onUploadSubmit: (e?: Event) => void;
  onRequestRemove: (name: string) => void;
  onCancelRemove: () => void;
  onConfirmRemove: () => void;
  toggleSelect: (name: string) => void;
  selectAll: () => void;
  clearSelection: () => void;
  onRequestBulkDelete: () => void;
  onCancelBulkDelete: () => void;
  onConfirmBulkDelete: () => void;
  refetch: () => void;
  clearNotice: () => void;
}

export function useMediaCategory({
  fetchList,
  install,
  remove,
  bulkDelete,
  accept,
}: UseMediaCategoryOptions): UseMediaCategory {
  const [state, setState] = useState<LoadState>({ tag: "loading" });
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<{
    current: number;
    total: number;
  } | null>(null);
  const [uploadItems, setUploadItems] = useState<UploadItem[]>([]);
  const [uploadFail, setUploadFail] = useState<MediaFailure | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmRemoveName, setConfirmRemoveName] = useState<string | null>(
    null,
  );
  const [removing, setRemoving] = useState(false);
  const [removeFail, setRemoveFail] = useState<MediaFailure | null>(null);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [confirmBulk, setConfirmBulk] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [bulkFail, setBulkFail] = useState<MediaFailure | null>(null);

  const uploadAbortRef = useRef<AbortController | null>(null);
  const removeAbortRef = useRef<AbortController | null>(null);
  const bulkAbortRef = useRef<AbortController | null>(null);
  const listAbortRef = useRef<AbortController | null>(null);

  function doFetch(signal?: AbortSignal) {
    fetchList(signal)
      .then((ml: MediaList) => {
        setState({ tag: "ready", items: ml.items });
        // Drop any selections that no longer exist after a refresh.
        setSelected((prev) => {
          if (prev.size === 0) return prev;
          const names = new Set(ml.items.map((i) => i.name));
          const next = new Set<string>();
          for (const n of prev) if (names.has(n)) next.add(n);
          return next;
        });
      })
      .catch(() => {
        if (!signal?.aborted) setState({ tag: "error" });
      });
  }

  useEffect(() => {
    const ctrl = new AbortController();
    listAbortRef.current = ctrl;
    doFetch(ctrl.signal);
    return () => {
      ctrl.abort();
      listAbortRef.current = null;
      uploadAbortRef.current?.abort();
      removeAbortRef.current?.abort();
      bulkAbortRef.current?.abort();
    };
  }, []);

  // Realtime: silently refetch the list whenever webd reports a catalog change
  // (an install/delete landed and was indexed). This replaces the old
  // single-shot post-mutation fetch that left the list stale until manual
  // refresh — now any change, including one from another tab, lands promptly.
  const silentRefetchRef = useRef<() => void>(() => {});
  silentRefetchRef.current = () => {
    const ctrl = new AbortController();
    listAbortRef.current?.abort();
    listAbortRef.current = ctrl;
    doFetch(ctrl.signal);
  };
  useEffect(
    () => subscribeMediaEvents(() => silentRefetchRef.current()),
    [],
  );

  /** Filter incoming files to the accepted extensions and de-dupe by name+size. */
  function acceptFiles(incoming: File[]) {
    const exts = accept?.map((e) => e.toLowerCase());
    setSelectedFiles((prev) => {
      const seen = new Set(prev.map((f) => `${f.name}:${f.size}`));
      const next = [...prev];
      for (const f of incoming) {
        if (exts && exts.length > 0) {
          const lower = f.name.toLowerCase();
          if (!exts.some((ext) => lower.endsWith(ext))) continue;
        }
        const key = `${f.name}:${f.size}`;
        if (seen.has(key)) continue;
        seen.add(key);
        next.push(f);
      }
      return next;
    });
  }

  function onFileChange(e: Event) {
    setUploadFail(null);
    setNotice(null);
    const input = e.currentTarget as HTMLInputElement;
    const files = input.files ? Array.from(input.files) : [];
    acceptFiles(files);
    // Clear so re-selecting the same file fires another change.
    input.value = "";
  }

  function onFilesDropped(files: File[]) {
    if (uploading) return;
    setUploadFail(null);
    setNotice(null);
    acceptFiles(files);
  }

  function removeStagedFile(name: string) {
    if (uploading) return;
    setSelectedFiles((prev) => prev.filter((f) => f.name !== name));
  }

  async function onUploadSubmit(e?: Event) {
    e?.preventDefault();
    if (selectedFiles.length === 0 || uploading) return;
    const files = selectedFiles;
    setUploading(true);
    setUploadFail(null);
    setNotice(null);
    setUploadItems(
      files.map((f) => ({ name: f.name, size: f.size, status: "pending" })),
    );
    setUploadProgress({ current: 0, total: files.length });

    const ac = new AbortController();
    uploadAbortRef.current = ac;

    const succeeded: string[] = [];
    const failed: { name: string; fail: MediaFailure }[] = [];
    let anyQueued = false;

    for (let i = 0; i < files.length; i++) {
      if (ac.signal.aborted) break;
      const f = files[i];
      setUploadItems((prev) =>
        prev.map((it) =>
          it.name === f.name ? { ...it, status: "uploading" } : it,
        ),
      );
      setUploadProgress({ current: i + 1, total: files.length });
      try {
        const res = await install(f, ac.signal);
        if (isQueued(res)) anyQueued = true;
        succeeded.push(f.name);
        setUploadItems((prev) =>
          prev.map((it) =>
            it.name === f.name ? { ...it, status: "done" } : it,
          ),
        );
        // Live update: refetch after each file so it appears in the list
        // immediately (the SSE catalog-change stream also nudges this).
        const refetchCtrl = new AbortController();
        listAbortRef.current?.abort();
        listAbortRef.current = refetchCtrl;
        doFetch(refetchCtrl.signal);
      } catch (err) {
        if (ac.signal.aborted) break;
        const fail = classifyMediaFailure(err);
        failed.push({ name: f.name, fail });
        setUploadItems((prev) =>
          prev.map((it) =>
            it.name === f.name
              ? { ...it, status: "error", error: fail.message }
              : it,
          ),
        );
      }
    }

    if (uploadAbortRef.current === ac) uploadAbortRef.current = null;
    if (ac.signal.aborted) {
      setUploading(false);
      return;
    }

    // Keep only the failed files staged so Retry re-runs just those.
    const failedNames = new Set(failed.map((x) => x.name));
    setSelectedFiles((prev) => prev.filter((f) => failedNames.has(f.name)));
    if (fileInputRef.current) fileInputRef.current.value = "";

    if (succeeded.length > 0) {
      setNotice(
        succeeded.length === 1
          ? anyQueued
            ? `Saved "${succeeded[0]}" — syncing to the car.`
            : `Uploaded "${succeeded[0]}".`
          : anyQueued
            ? `Saved ${succeeded.length} files — syncing to the car.`
            : `Uploaded ${succeeded.length} files.`,
      );
    }
    if (failed.length > 0) {
      setUploadFail({
        message:
          failed.length === 1
            ? failed[0].fail.message
            : `${failed.length} file(s) failed to upload. ${failed[0].fail.message}`,
        retryable: failed.some((x) => x.fail.retryable),
      });
    } else {
      setUploadItems([]);
    }

    setUploadProgress(null);
    setUploading(false);
  }

  function onRequestRemove(name: string) {
    setConfirmRemoveName(name);
    setRemoveFail(null);
  }

  function onCancelRemove() {
    setConfirmRemoveName(null);
  }

  async function onConfirmRemove() {
    const name = confirmRemoveName;
    if (!name || removing) return;
    setRemoving(true);
    setRemoveFail(null);
    const ac = new AbortController();
    removeAbortRef.current = ac;
    try {
      const res = await remove(name, ac.signal);
      setConfirmRemoveName(null);
      setNotice(
        isQueued(res)
          ? `Removing "${name}" — syncing to the car.`
          : `Removed "${name}".`,
      );
      const refetchCtrl = new AbortController();
      listAbortRef.current?.abort();
      listAbortRef.current = refetchCtrl;
      doFetch(refetchCtrl.signal);
    } catch (err) {
      if (ac.signal.aborted) return;
      setRemoveFail(classifyMediaFailure(err));
    } finally {
      if (removeAbortRef.current === ac) removeAbortRef.current = null;
      setRemoving(false);
    }
  }

  function refetch() {
    const ctrl = new AbortController();
    listAbortRef.current?.abort();
    listAbortRef.current = ctrl;
    setState({ tag: "loading" });
    doFetch(ctrl.signal);
  }

  function toggleSelect(name: string) {
    setBulkFail(null);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  function selectAll() {
    setBulkFail(null);
    setSelected(
      state.tag === "ready"
        ? new Set(state.items.map((i) => i.name))
        : new Set(),
    );
  }

  function clearSelection() {
    setSelected(new Set());
  }

  function onRequestBulkDelete() {
    if (selected.size === 0) return;
    setConfirmBulk(true);
    setBulkFail(null);
  }

  function onCancelBulkDelete() {
    setConfirmBulk(false);
  }

  async function onConfirmBulkDelete() {
    if (!bulkDelete || bulkDeleting || selected.size === 0) return;
    setBulkDeleting(true);
    setBulkFail(null);
    const names = [...selected];
    const ac = new AbortController();
    bulkAbortRef.current = ac;
    try {
      const res = await bulkDelete(names, ac.signal);
      setConfirmBulk(false);
      setSelected(new Set());
      const queued = isQueued(res);
      setNotice(
        names.length === 1
          ? queued
            ? `Removing "${names[0]}" — syncing to the car.`
            : `Removed "${names[0]}".`
          : queued
            ? `Removing ${names.length} items — syncing to the car.`
            : `Removed ${names.length} items.`,
      );
      const refetchCtrl = new AbortController();
      listAbortRef.current?.abort();
      listAbortRef.current = refetchCtrl;
      doFetch(refetchCtrl.signal);
    } catch (err) {
      if (ac.signal.aborted) return;
      setBulkFail(classifyMediaFailure(err));
    } finally {
      if (bulkAbortRef.current === ac) bulkAbortRef.current = null;
      setBulkDeleting(false);
    }
  }

  return {
    state,
    fileInputRef,
    selectedFiles,
    uploading,
    uploadProgress,
    uploadItems,
    uploadFail,
    notice,
    confirmRemoveName,
    removing,
    removeFail,
    bulkEnabled: bulkDelete != null,
    selected,
    confirmBulk,
    bulkDeleting,
    bulkFail,
    onFileChange,
    onFilesDropped,
    removeStagedFile,
    onUploadSubmit,
    onRequestRemove,
    onCancelRemove,
    onConfirmRemove,
    toggleSelect,
    selectAll,
    clearSelection,
    onRequestBulkDelete,
    onCancelBulkDelete,
    onConfirmBulkDelete,
    refetch,
    clearNotice: () => setNotice(null),
  };
}
