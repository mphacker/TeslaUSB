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
import { ApiError } from "../api/client";
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
  /** `POST` install function. */
  install: (file: File | Blob, signal?: AbortSignal) => Promise<unknown>;
  /** `DELETE` remove function. */
  remove: (name: string, signal?: AbortSignal) => Promise<unknown>;
}

export interface UseMediaCategory {
  state: LoadState;
  // Upload
  fileInputRef: RefObject<HTMLInputElement>;
  selectedFile: File | null;
  uploading: boolean;
  uploadFail: MediaFailure | null;
  notice: string | null;
  // Remove
  confirmRemoveName: string | null;
  removing: boolean;
  removeFail: MediaFailure | null;
  // Handlers
  onFileChange: (e: Event) => void;
  onUploadSubmit: (e: Event) => void;
  onRequestRemove: (name: string) => void;
  onCancelRemove: () => void;
  onConfirmRemove: () => void;
  refetch: () => void;
  clearNotice: () => void;
}

export function useMediaCategory({
  fetchList,
  install,
  remove,
}: UseMediaCategoryOptions): UseMediaCategory {
  const [state, setState] = useState<LoadState>({ tag: "loading" });
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadFail, setUploadFail] = useState<MediaFailure | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmRemoveName, setConfirmRemoveName] = useState<string | null>(
    null,
  );
  const [removing, setRemoving] = useState(false);
  const [removeFail, setRemoveFail] = useState<MediaFailure | null>(null);

  const uploadAbortRef = useRef<AbortController | null>(null);
  const removeAbortRef = useRef<AbortController | null>(null);
  const listAbortRef = useRef<AbortController | null>(null);

  function doFetch(signal?: AbortSignal) {
    fetchList(signal)
      .then((ml: MediaList) => {
        setState({ tag: "ready", items: ml.items });
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
    };
  }, []);

  function onFileChange(e: Event) {
    setUploadFail(null);
    setNotice(null);
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0] ?? null;
    setSelectedFile(file);
  }

  async function onUploadSubmit(e: Event) {
    e.preventDefault();
    if (!selectedFile || uploading) return;
    setUploading(true);
    setUploadFail(null);
    setNotice(null);
    const ac = new AbortController();
    uploadAbortRef.current = ac;
    try {
      await install(selectedFile, ac.signal);
      const name = selectedFile.name;
      setSelectedFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setNotice(`Installed "${name}".`);
      const refetchCtrl = new AbortController();
      listAbortRef.current?.abort();
      listAbortRef.current = refetchCtrl;
      doFetch(refetchCtrl.signal);
    } catch (err) {
      if (ac.signal.aborted) return;
      setUploadFail(classifyMediaFailure(err));
    } finally {
      if (uploadAbortRef.current === ac) uploadAbortRef.current = null;
      setUploading(false);
    }
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
      await remove(name, ac.signal);
      setConfirmRemoveName(null);
      setNotice(`Removed "${name}".`);
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

  return {
    state,
    fileInputRef,
    selectedFile,
    uploading,
    uploadFail,
    notice,
    confirmRemoveName,
    removing,
    removeFail,
    onFileChange,
    onUploadSubmit,
    onRequestRemove,
    onCancelRemove,
    onConfirmRemove,
    refetch,
    clearNotice: () => setNotice(null),
  };
}
