/**
 * Music-specific hook that owns the folder-tree model, navigation, and all
 * music mutations with bounded poll-until-convergence auto-refresh.
 *
 * Does NOT touch useMediaCategory — the other 4 media screens are unaffected.
 *
 * Convergence pattern mirrors ChimeScheduler exactly:
 *   POLL_INTERVAL_MS = 2 s, POLL_MAX_MS = 45 s, per-op startedAt budget,
 *   "waiting" phase + "Refresh now" affordance on timeout, AbortController
 *   cleanup, stable per-op tokens so concurrent ops don't reset each other.
 */

import { useEffect, useRef, useState } from "preact/hooks";
import type { RefObject } from "preact";
import { api } from "../api/client";
import { subscribeMediaEvents } from "../api/mediaEvents";
import { classifyMediaFailure } from "./useMediaCategory";
import type { MediaFailure } from "./useMediaCategory";
import type { MediaItem } from "../api/types";

export type { MediaItem, MediaFailure };
export { classifyMediaFailure };

// ── Constants ────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 2000;
const POLL_MAX_MS = 45000;

const MUSIC_EXTENSIONS = ["mp3", "flac", "wav", "aac", "m4a"];

function isAllowedMusicFile(f: File): boolean {
  const dot = f.name.lastIndexOf(".");
  if (dot < 0) return false;
  return MUSIC_EXTENSIONS.includes(f.name.slice(dot + 1).toLowerCase());
}

// ── Types ─────────────────────────────────────────────────────────────────────

export type LibraryLoadState =
  | { tag: "loading" }
  | { tag: "error" }
  | { tag: "ready" };

export type PendingOpKind =
  | "upload"
  | "createFolder"
  | "deleteFolder"
  | "deleteFiles"
  | "move";

export type PendingPhase = "syncing" | "waiting";

export interface PendingOp {
  token: number;
  kind: PendingOpKind;
  phase: PendingPhase;
  startedAt: number;
  /** Human-readable label shown in the optimistic UI (filename / folder name). */
  label: string;
  /**
   * Primary convergence path (full rel_path including "Music/" prefix):
   * - upload / createFolder: path to watch for PRESENCE
   * - deleteFolder: prefix to watch for ABSENCE
   */
  targetPath: string;
  /** deleteFiles: rel_paths that must all become ABSENT. */
  filePaths?: readonly string[];
  /** move: source rel_path that must become ABSENT. */
  moveFrom?: string;
  /** move: dest rel_path that must become PRESENT. */
  moveTo?: string;
  /** move: two-phase state — "copying" = phase 1 (wait for dest), "deleting" = phase 2 (wait for source gone). */
  movePhase?: "copying" | "deleting";
  /** move: expected size_bytes of the destination; guards against a partial write in phase 1. */
  moveExpectedSize?: number;
}

// ── Folder-model helpers ─────────────────────────────────────────────────────

function isHidden(segment: string): boolean {
  return segment.startsWith(".");
}

function pathPrefix(segments: string[]): string {
  return segments.length === 0
    ? "Music/"
    : `Music/${segments.join("/")}/`;
}

/** Strip the leading "Music/" from a rel_path. */
function stripMusicPrefix(relPath: string): string {
  return relPath.startsWith("Music/") ? relPath.slice(6) : relPath;
}

/** Unique non-hidden subfolders directly under `currentPath`. */
export function getSubfolders(
  items: MediaItem[],
  currentPath: string[],
): string[] {
  const prefix = pathPrefix(currentPath);
  const seen = new Set<string>();
  for (const item of items) {
    if (!item.rel_path.startsWith(prefix)) continue;
    const rest = item.rel_path.slice(prefix.length);
    const slash = rest.indexOf("/");
    if (slash === -1) continue; // direct file, not a subfolder indicator
    const seg = rest.slice(0, slash);
    if (!isHidden(seg)) seen.add(seg);
  }
  return [...seen].sort();
}

/** Non-hidden files directly in `currentPath` (no deeper). */
export function getCurrentFiles(
  items: MediaItem[],
  currentPath: string[],
): MediaItem[] {
  const prefix = pathPrefix(currentPath);
  return items.filter((item) => {
    if (!item.rel_path.startsWith(prefix)) return false;
    const rest = item.rel_path.slice(prefix.length);
    return !rest.includes("/") && !isHidden(rest);
  });
}

/** All folder paths (relative under Music/) visible anywhere in the library.
 *  Includes "" for the root. Used by the move-to dialog. */
export function getAllFolderPaths(items: MediaItem[]): string[] {
  const seen = new Set<string>([""]); // "" = root
  for (const item of items) {
    if (!item.rel_path.startsWith("Music/")) continue;
    const rest = stripMusicPrefix(item.rel_path);
    const parts = rest.split("/");
    for (let i = 1; i < parts.length; i++) {
      const segs = parts.slice(0, i);
      if (segs.some((s) => isHidden(s))) break;
      seen.add(segs.join("/"));
    }
  }
  return [...seen].sort();
}

/** Total count of non-hidden files at any depth (for the Files stat tile). */
export function countFiles(items: MediaItem[]): number {
  return items.filter((item) => {
    if (!item.rel_path.startsWith("Music/")) return false;
    const parts = stripMusicPrefix(item.rel_path).split("/");
    return !parts.some((p) => isHidden(p));
  }).length;
}

// ── Convergence check ─────────────────────────────────────────────────────────

function hasConverged(op: PendingOp, items: MediaItem[]): boolean {
  const relPaths = new Set(items.map((i) => i.rel_path));
  switch (op.kind) {
    case "upload":
      return relPaths.has(op.targetPath);
    case "createFolder":
      // Any item under the folder = the folder was created
      return items.some((i) => i.rel_path.startsWith(op.targetPath + "/"));
    case "deleteFolder":
      return !items.some((i) => i.rel_path.startsWith(op.targetPath + "/"));
    case "deleteFiles":
      return (op.filePaths ?? []).every((p) => !relPaths.has(p));
    case "move":
      // Move is two-phase (copy → delete source); convergence is driven by the
      // poll loop directly so this path is never reached for move ops.
      return false;
  }
}

function makeConvergenceNotice(op: PendingOp): string {
  const n = (op.filePaths ?? []).length;
  switch (op.kind) {
    case "upload":
      return `"${op.label}" uploaded.`;
    case "createFolder":
      return `Folder "${op.label}" created.`;
    case "deleteFolder":
      return `Folder "${op.label}" deleted.`;
    case "deleteFiles":
      return n === 1 ? `"${op.label}" deleted.` : `${n} files deleted.`;
    case "move":
      return `Moved "${op.label}".`;
  }
}

// ── Token factory (module-level counter; resets on HMR, fine for tests) ──────

let _tok = 0;
function nextToken(): number {
  return ++_tok;
}

// ── Hook return type ──────────────────────────────────────────────────────────

export interface UseMusicLibrary {
  // Load state
  state: LibraryLoadState;
  allItems: MediaItem[];

  // Navigation
  currentPath: string[];
  subfolders: string[];
  currentFiles: MediaItem[];
  totalFileCount: number;
  availableFolders: string[];
  breadcrumbs: Array<{ label: string; index: number }>;
  enterFolder: (name: string) => void;
  navigateTo: (index: number) => void;

  // Upload
  fileInputRef: RefObject<HTMLInputElement>;
  selectedFiles: File[];
  uploading: boolean;
  uploadProgress: { done: number; total: number } | null;
  uploadFail: MediaFailure | null;
  onFileChange: (e: Event) => void;
  onFilesDropped: (files: File[]) => void;
  onUploadSubmit: () => void;
  clearSelectedFile: () => void;

  // Create folder
  createFolderBusy: boolean;
  createFolderFail: MediaFailure | null;
  onCreateFolder: (name: string) => void;

  // Delete folder
  onDeleteFolder: (name: string) => void;

  // File multi-select + delete
  selected: ReadonlySet<string>;
  toggleSelect: (relPath: string) => void;
  selectAll: () => void;
  clearSelection: () => void;
  onDeleteFiles: (relPaths: string[]) => void;
  deletingFiles: boolean;
  deleteFilesFail: MediaFailure | null;

  // Move file
  moveDialogRelPath: string | null;
  openMoveDialog: (relPath: string) => void;
  closeMoveDialog: () => void;
  onConfirmMove: (destFolderPath: string) => void;
  moveBusy: boolean;
  moveFail: MediaFailure | null;

  // Optimistic / pending ops
  pendingOps: readonly PendingOp[];
  hasWaiting: boolean;

  // Notices & refresh
  notice: string | null;
  clearNotice: () => void;
  refetch: () => void;
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useMusicLibrary(): UseMusicLibrary {
  const [state, setState] = useState<LibraryLoadState>({ tag: "loading" });
  const [items, setItems] = useState<MediaItem[]>([]);
  const [currentPath, setCurrentPath] = useState<string[]>([]);

  // Upload
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<{
    done: number;
    total: number;
  } | null>(null);
  const [uploadFail, setUploadFail] = useState<MediaFailure | null>(null);

  // Create folder
  const [createFolderBusy, setCreateFolderBusy] = useState(false);
  const [createFolderFail, setCreateFolderFail] = useState<MediaFailure | null>(null);

  // Multi-select + delete
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [deletingFiles, setDeletingFiles] = useState(false);
  const [deleteFilesFail, setDeleteFilesFail] = useState<MediaFailure | null>(null);

  // Move
  const [moveDialogRelPath, setMoveDialogRelPath] = useState<string | null>(null);
  const [moveBusy, setMoveBusy] = useState(false);
  const [moveFail, setMoveFail] = useState<MediaFailure | null>(null);

  // Pending ops + polling
  const [pendingOps, setPendingOps] = useState<PendingOp[]>([]);
  const [pollToken, setPollToken] = useState(0);
  const pendingOpsRef = useRef<PendingOp[]>([]);
  // Guards the phase-1 → phase-2 source-delete so it fires exactly once per op token.
  const deleteFiredRef = useRef<Set<number>>(new Set());

  const [notice, setNotice] = useState<string | null>(null);

  // List fetch abort ref
  const listAbortRef = useRef<AbortController | null>(null);

  // Keep ref in sync with state (read in async poll callbacks)
  useEffect(() => {
    pendingOpsRef.current = pendingOps;
  }, [pendingOps]);

  // ── Initial load ────────────────────────────────────────────────────────────

  function doFetch(signal?: AbortSignal) {
    api
      .music(signal)
      .then((list) => {
        setItems(list.items);
        setState({ tag: "ready" });
        // Drop selected paths that no longer exist.
        setSelected((prev) => {
          if (prev.size === 0) return prev;
          const present = new Set(list.items.map((i) => i.rel_path));
          const next = new Set<string>();
          for (const p of prev) if (present.has(p)) next.add(p);
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
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Realtime: when webd reports a catalog change (an upload/create/move/delete
  // landed and was indexed), refetch the list immediately so new music appears
  // without waiting for the convergence poll or a manual refresh. The
  // convergence poll below still owns clearing the per-op "syncing" badges.
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

  // ── Poll-until-convergence (mirrors ChimeScheduler exactly) ─────────────────
  // Fires whenever pollToken increments (i.e., after each 202 response).
  // Each run polls GET /api/music every POLL_INTERVAL_MS until ALL syncing ops
  // have either converged (removed from pendingOps) or timed out (→ "waiting").
  // Per-op startedAt ensures siblings never borrow each other's 45 s budget.

  useEffect(() => {
    if (pollToken === 0) return;
    if (!pendingOpsRef.current.some((op) => op.phase === "syncing")) return;

    let cancelled = false;
    const ctrl = new AbortController();
    let pollId: ReturnType<typeof setTimeout> | null = null;

    const stop = () => {
      if (pollId !== null) clearTimeout(pollId);
      pollId = null;
    };

    const runPoll = async () => {
      if (cancelled) return;

      let newItems: MediaItem[] | null = null;
      try {
        const list = await api.music(ctrl.signal);
        newItems = list.items;
      } catch {
        // network blip — keep polling while ops are within budget
      }
      if (cancelled) return;

      const now = Date.now();
      const ops = pendingOpsRef.current;
      const convergedTokens = new Set<number>();
      const notices: string[] = [];

      if (newItems !== null) {
        setItems(newItems);
        for (const op of ops) {
          if (op.phase !== "syncing") continue;
          if (op.kind === "move") {
            if (op.movePhase === "copying") {
              // Phase 1: destination must be present with the expected size.
              const destItem = newItems.find((i) => i.rel_path === op.moveTo);
              if (
                destItem !== undefined &&
                destItem.size_bytes === op.moveExpectedSize
              ) {
                // Fire the source-delete exactly once. The request is
                // deliberately NOT tied to the poll AbortController: a
                // pollToken change or unmount must not cancel an in-flight
                // source-delete (that would strand the move as a permanent
                // duplicate). We advance to phase 2 only once the delete is
                // accepted; if it fails we clear the guard so a later poll
                // retries.
                if (!deleteFiredRef.current.has(op.token)) {
                  deleteFiredRef.current.add(op.token);
                  const tok = op.token;
                  api
                    .musicDeleteFiles([stripMusicPrefix(op.moveFrom!)])
                    .then(() => {
                      setPendingOps((prev) =>
                        prev.map((o) =>
                          o.token === tok
                            ? { ...o, movePhase: "deleting" as const }
                            : o,
                        ),
                      );
                      triggerPoll();
                    })
                    .catch(() => {
                      deleteFiredRef.current.delete(tok);
                    });
                }
              }
            } else if (op.movePhase === "deleting") {
              // Phase 2: source must be absent.
              const present = new Set(newItems.map((i) => i.rel_path));
              if (!present.has(op.moveFrom!)) {
                convergedTokens.add(op.token);
                notices.push(makeConvergenceNotice(op));
              }
            }
          } else if (hasConverged(op, newItems)) {
            convergedTokens.add(op.token);
            notices.push(makeConvergenceNotice(op));
          }
        }
      }

      // Remove fully-converged ops; transition budget-elapsed "syncing" ops
      // → "waiting". (copy→deleting is handled in the source-delete callback.)
      setPendingOps((prev) =>
        prev
          .filter((op) => !convergedTokens.has(op.token))
          .map((op) => {
            if (
              op.phase === "syncing" &&
              now - op.startedAt >= POLL_MAX_MS - POLL_INTERVAL_MS
            ) {
              return { ...op, phase: "waiting" as PendingPhase };
            }
            return op;
          }),
      );

      if (notices.length > 0) setNotice(notices.join(" "));

      // Keep polling while any "syncing" op is still within budget
      const stillActive = ops
        .filter((op) => !convergedTokens.has(op.token))
        .some(
          (op) =>
            op.phase === "syncing" &&
            now - op.startedAt < POLL_MAX_MS - POLL_INTERVAL_MS,
        );

      if (stillActive && !cancelled) {
        pollId = setTimeout(() => void runPoll(), POLL_INTERVAL_MS);
      } else {
        stop();
      }
    };

    pollId = setTimeout(() => void runPoll(), POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      ctrl.abort();
      stop();
    };
  }, [pollToken]); // eslint-disable-line react-hooks/exhaustive-deps

  function triggerPoll() {
    setPollToken((t) => t + 1);
  }

  function addPendingOp(
    op: Omit<PendingOp, "token" | "phase" | "startedAt">,
  ): PendingOp {
    const full: PendingOp = {
      ...op,
      token: nextToken(),
      phase: "syncing",
      startedAt: Date.now(),
    };
    setPendingOps((prev) => [...prev, full]);
    return full;
  }

  // ── Navigation ───────────────────────────────────────────────────────────────

  function enterFolder(name: string) {
    setCurrentPath((prev) => [...prev, name]);
  }

  function navigateTo(index: number) {
    setCurrentPath((prev) => (index < 0 ? [] : prev.slice(0, index + 1)));
  }

  // ── Upload ───────────────────────────────────────────────────────────────────

  function acceptFiles(incoming: File[]) {
    setUploadFail(null);
    const allowed = incoming.filter(isAllowedMusicFile);
    const rejected = incoming.length - allowed.length;
    if (rejected > 0) {
      setUploadFail({
        message: `${rejected} file${rejected === 1 ? "" : "s"} skipped — only mp3, flac, wav, aac, and m4a are allowed.`,
        retryable: false,
      });
    }
    if (allowed.length === 0) return;
    setSelectedFiles((prev) => {
      const seen = new Set(prev.map((f) => `${f.name}:${f.size}`));
      const merged = [...prev];
      for (const f of allowed) {
        const key = `${f.name}:${f.size}`;
        if (!seen.has(key)) {
          seen.add(key);
          merged.push(f);
        }
      }
      return merged;
    });
  }

  function onFileChange(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    acceptFiles(Array.from(input.files ?? []));
    // Reset so re-selecting the same file(s) fires onChange again.
    input.value = "";
  }

  function onFilesDropped(files: File[]) {
    if (uploading) return;
    acceptFiles(files);
  }

  async function onUploadSubmit() {
    if (selectedFiles.length === 0 || uploading) return;
    setUploading(true);
    setUploadFail(null);
    const files = selectedFiles;
    const folder =
      currentPath.length === 0 ? undefined : currentPath.join("/");
    const ac = new AbortController();
    setUploadProgress({ done: 0, total: files.length });
    const remaining: File[] = [];
    let firstFail: MediaFailure | null = null;
    let queuedAny = false;
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        try {
          await api.installMusic(file, ac.signal, folder);
          const targetPath = folder
            ? `Music/${folder}/${file.name}`
            : `Music/${file.name}`;
          addPendingOp({ kind: "upload", label: file.name, targetPath });
          queuedAny = true;
        } catch (err) {
          if (ac.signal.aborted) return;
          remaining.push(file);
          if (!firstFail) firstFail = classifyMediaFailure(err);
        }
        setUploadProgress({ done: i + 1, total: files.length });
      }
      setSelectedFiles(remaining);
      if (fileInputRef.current) fileInputRef.current.value = "";
      if (firstFail) {
        setUploadFail(
          remaining.length === files.length
            ? firstFail
            : {
                message: `${remaining.length} of ${files.length} file${files.length === 1 ? "" : "s"} failed: ${firstFail.message}`,
                retryable: firstFail.retryable,
              },
        );
      }
      if (queuedAny) triggerPoll();
    } finally {
      setUploading(false);
      setUploadProgress(null);
    }
  }

  function clearSelectedFile() {
    setSelectedFiles([]);
    setUploadFail(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  // ── Create folder ─────────────────────────────────────────────────────────────

  async function onCreateFolder(name: string) {
    const trimmed = name.trim();
    if (!trimmed || createFolderBusy) return;
    setCreateFolderBusy(true);
    setCreateFolderFail(null);
    const path =
      currentPath.length === 0
        ? trimmed
        : `${currentPath.join("/")}/${trimmed}`;
    const ac = new AbortController();
    try {
      await api.musicCreateFolder(path, ac.signal);
      const targetPath = `Music/${path}`;
      addPendingOp({ kind: "createFolder", label: trimmed, targetPath });
      triggerPoll();
    } catch (err) {
      if (ac.signal.aborted) return;
      setCreateFolderFail(classifyMediaFailure(err));
    } finally {
      setCreateFolderBusy(false);
    }
  }

  // ── Delete folder ─────────────────────────────────────────────────────────────

  async function onDeleteFolder(name: string) {
    const path =
      currentPath.length === 0
        ? name
        : `${currentPath.join("/")}/${name}`;
    const ac = new AbortController();
    try {
      await api.musicDeleteFolder(path, ac.signal);
      const targetPath = `Music/${path}`;
      addPendingOp({ kind: "deleteFolder", label: name, targetPath });
      triggerPoll();
    } catch (err) {
      if (ac.signal.aborted) return;
      setNotice(classifyMediaFailure(err).message);
    }
  }

  // ── Delete files ──────────────────────────────────────────────────────────────

  async function onDeleteFiles(relPaths: string[]) {
    if (relPaths.length === 0 || deletingFiles) return;
    setDeletingFiles(true);
    setDeleteFilesFail(null);
    const ac = new AbortController();
    try {
      await api.musicDeleteFiles(relPaths.map(stripMusicPrefix), ac.signal);
      // Clear the deleted paths from selection immediately
      setSelected((prev) => {
        if (prev.size === 0) return prev;
        const next = new Set(prev);
        for (const p of relPaths) next.delete(p);
        return next;
      });
      const firstName =
        relPaths[0].split("/").pop() ?? relPaths[0];
      const label =
        relPaths.length === 1 ? firstName : `${relPaths.length} files`;
      addPendingOp({
        kind: "deleteFiles",
        label,
        targetPath: relPaths[0],
        filePaths: relPaths,
      });
      triggerPoll();
    } catch (err) {
      if (ac.signal.aborted) return;
      setDeleteFilesFail(classifyMediaFailure(err));
    } finally {
      setDeletingFiles(false);
    }
  }

  // ── Move file ─────────────────────────────────────────────────────────────────

  function openMoveDialog(relPath: string) {
    setMoveDialogRelPath(relPath);
    setMoveFail(null);
  }

  function closeMoveDialog() {
    setMoveDialogRelPath(null);
    setMoveFail(null);
  }

  async function onConfirmMove(destFolderPath: string) {
    if (!moveDialogRelPath || moveBusy) return;
    const filename = moveDialogRelPath.split("/").pop() ?? "";
    // `from` and `to` are subpaths relative under Music/ (no "Music/" prefix)
    const from = stripMusicPrefix(moveDialogRelPath);
    const to = destFolderPath ? `${destFolderPath}/${filename}` : filename;
    setMoveBusy(true);
    setMoveFail(null);
    const ac = new AbortController();
    try {
      await api.musicMove(from, to, ac.signal);
      closeMoveDialog();
      const sourceItem = items.find((i) => i.rel_path === moveDialogRelPath);
      addPendingOp({
        kind: "move",
        label: filename,
        targetPath: `Music/${to}`,
        moveFrom: moveDialogRelPath,
        moveTo: `Music/${to}`,
        movePhase: "copying",
        moveExpectedSize: sourceItem?.size_bytes ?? 0,
      });
      triggerPoll();
    } catch (err) {
      if (ac.signal.aborted) return;
      setMoveFail(classifyMediaFailure(err));
    } finally {
      setMoveBusy(false);
    }
  }

  // ── Manual refresh ────────────────────────────────────────────────────────────

  function refetch() {
    const ctrl = new AbortController();
    listAbortRef.current?.abort();
    listAbortRef.current = ctrl;
    setState({ tag: "loading" });
    // Discard "waiting" ops on a manual refresh; leave "syncing" ones alone.
    setPendingOps((prev) => prev.filter((op) => op.phase !== "waiting"));
    doFetch(ctrl.signal);
  }

  // ── Derived state ─────────────────────────────────────────────────────────────

  const subfolders =
    state.tag === "ready" ? getSubfolders(items, currentPath) : [];
  const currentFiles =
    state.tag === "ready" ? getCurrentFiles(items, currentPath) : [];
  const totalFileCount = state.tag === "ready" ? countFiles(items) : 0;
  const availableFolders =
    state.tag === "ready" ? getAllFolderPaths(items) : [""];
  const breadcrumbs = currentPath.map((label, index) => ({ label, index }));
  const hasWaiting = pendingOps.some((op) => op.phase === "waiting");

  return {
    state,
    allItems: items,
    currentPath,
    subfolders,
    currentFiles,
    totalFileCount,
    availableFolders,
    breadcrumbs,
    enterFolder,
    navigateTo,
    fileInputRef,
    selectedFiles,
    uploading,
    uploadProgress,
    uploadFail,
    onFileChange,
    onFilesDropped,
    onUploadSubmit,
    clearSelectedFile,
    createFolderBusy,
    createFolderFail,
    onCreateFolder,
    onDeleteFolder,
    selected,
    toggleSelect: (relPath: string) =>
      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(relPath)) next.delete(relPath);
        else next.add(relPath);
        return next;
      }),
    selectAll: () =>
      setSelected(new Set(currentFiles.map((f) => f.rel_path))),
    clearSelection: () => setSelected(new Set()),
    onDeleteFiles,
    deletingFiles,
    deleteFilesFail,
    moveDialogRelPath,
    openMoveDialog,
    closeMoveDialog,
    onConfirmMove,
    moveBusy,
    moveFail,
    pendingOps,
    hasWaiting,
    notice,
    clearNotice: () => setNotice(null),
    refetch,
  };
}
