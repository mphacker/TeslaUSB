import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { MediaPills } from "../components/MediaPills";
import { Icon } from "../components/Icon";
import { ChimeAudioEditor } from "../components/ChimeAudioEditor";
import { BusyOverlay, useDelayedFlag } from "../components/BusyOverlay";
import { ChimeScheduler } from "./ChimeScheduler";
import { useFullWidthScreen } from "../hooks/useFullWidthScreen";
import { useFileDrop } from "../hooks/useFileDrop";
import { api, ApiError, CHIME_MAX_BYTES } from "../api/client";
import type { Chimes, InstalledChime, LibraryEntry } from "../api/types";
import "../styles/media.css";

/**
 * The Media section (route `/media`, Shell active "media").
 *
 * Parity target: the legacy Flask app's `/media/` 302-redirected to
 * `/lock_chimes/`, so the visible "media page" was the **Lock Chimes**
 * management screen — a media pill sub-nav (`media_hub_nav.html`:
 * Chimes/Music/Boombox/Shows/Wraps/Plates) over the lock-chime manager
 * (`lock_chimes.html`): an "Active Lock Chime" card, an "Upload New Chime"
 * panel, "Chime Scheduler" + "Random Chime Groups" panels, and a "Chime
 * Library" table. This screen reproduces that v1 look using the carried-over
 * legacy stylesheet (`/static/css/style.css`: `.media-pills`, `.media-pill`,
 * `.settings-section`, `.action-btn`, …) which the SPA already loads.
 *
 * Backend reality (B-1, intentionally honest — NOT fabricated):
 *  - `GET /api/chimes` (read-only) reports which lock chime is installed on the
 *    p2 MEDIA partition, routed through the scannerd→indexd→webd catalog (NOT
 *    the gadgetd eject-handoff). The "Active Lock Chime" and "Chime Library"
 *    sections render that live fact, degrading to honest empty/pending states
 *    (never fabricated rows) when nothing is installed or the catalog can't be
 *    read.
 *  - `POST /api/chimes` (install/replace `LockChime.wav`) and
 *    `DELETE /api/chimes/LockChime` route through the gadgetd eject-handoff that
 *    momentarily ejects the USB drive from the live vehicle. They are wired here
 *    as **deliberate, two-step operator actions** (pick-a-file → Upload; a named
 *    Remove → confirm dialog), mirroring the operator-gated clip-delete pattern:
 *    the WAV is validated client-side before upload, the handoff shows a busy
 *    state, and a transient `409 handoff_busy` / `503 gadgetd_unavailable`
 *    offers a friendly retry while validation/`4xx` errors are terminal.
 *  - B-1's lock chime is **single-slot** (`LockChime.wav`), so there is no
 *    multi-chime library, scheduler, or random-group backend. Those v1 panels
 *    render honest "not available in this build" states rather than inventing
 *    controls.
 */

const DASH = "\u2014";
const ACT_POLL_INTERVAL_MS = 2000;
const ACT_POLL_MAX_MS = 60000;
const REENUM_POLL_INTERVAL_MS = 2000;
const REENUM_SYNCING_MAX_MS = 30000;
const REENUM_POLL_MAX_MS = 120000;

function activationSuccessMessage(filename: string): string {
  return `“${filename}” is now your active lock chime.`;
}

function activationNoticeNextLock(filename: string): string {
  return `“${filename}” is now your active lock chime — your car will play it on the next lock.`;
}

/**
 * True once `GET /api/chimes` shows the activated chime has actually landed on
 * the car's `LockChime.wav`. The active file is ALWAYS named `LockChime.wav`,
 * so the filename can't distinguish chimes — we confirm a *rewrite* instead:
 * the size must match the activated file AND (nothing was active before, OR the
 * size changed, OR the mtime became readable/advanced). Requiring a change in
 * addition to a size match avoids a same-size old chime reading as "applied"
 * before the handoff has run.
 */
function activationConverged(
  inst: InstalledChime | null,
  pending: { bytes: number; preModified: string | null; preSize: number | null },
): boolean {
  if (!inst || inst.size_bytes !== pending.bytes) return false;
  if (pending.preSize == null) return true; // nothing was active before — a size match lands
  if (inst.size_bytes !== pending.preSize) return true; // size changed → rewritten
  if (pending.preModified == null) return inst.modified != null; // mtime became readable
  return inst.modified !== pending.preModified; // mtime advanced → rewritten
}

/**
 * Resolve the *source* name to show on the active card. The car's active file is
 * always literally `LockChime.wav`, which is meaningless to the operator — they
 * want to see which library chime they activated (e.g. `MarioFart.wav`). No
 * backend field carries this, so we resolve it client-side from the only signals
 * available, never overclaiming:
 *   1. The chime we just activated this session (exact operator intent), as long
 *      as its byte size still matches what's installed.
 *   2. Otherwise, a UNIQUE library entry whose byte size equals the installed
 *      chime (the activate copies bytes verbatim, so sizes match). A size
 *      collision (>1 match) or a chime not present in the library (0 matches)
 *      resolves to `null` → the card falls back to the honest `LockChime.wav`.
 */
function resolveActiveSourceName(
  installed: InstalledChime | null,
  library: LibraryEntry[],
  lastActivated: { filename: string; bytes: number } | null,
): string | null {
  if (!installed) return null;
  if (lastActivated && lastActivated.bytes === installed.size_bytes) {
    return lastActivated.filename;
  }
  const matches = library.filter((c) => c.bytes === installed.size_bytes);
  return matches.length === 1 ? matches[0].filename : null;
}

function buildId(): string {
  return (
    (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__ ??
    "dev"
  );
}

/**
 * Mirror webd's `sanitise_filename` (media_upload.rs): take the last path
 * component, then trim surrounding whitespace. webd stores — and the media
 * catalog therefore reports — this transformed name, so the pending-row
 * convergence key must match it (not the raw `File.name`) or a space-padded
 * upload would never converge on hardware. Rejected names (non-ASCII, etc.)
 * never reach here: `uploadLibraryChime` throws on the 422 first.
 */
function catalogChimeName(raw: string): string {
  return (raw.split(/[\\/]/).pop() ?? raw).trim();
}

interface PendingUploadState {
  filename: string;
  bytes: number;
  token: number;
}

function applySingleUploadSuccess(
  filename: string,
  bytes: number,
  setPendingUpload: (value: PendingUploadState | ((prev: PendingUploadState | null) => PendingUploadState | null)) => void,
  setNotice: (value: string | null | ((prev: string | null) => string | null)) => void,
) {
  setPendingUpload((prev) => ({
    filename,
    bytes,
    token: (prev?.token ?? 0) + 1,
  }));
  setNotice(`Upload accepted — syncing “${filename}” to your chime library below…`);
}

/** A lock-chime byte count → compact human string (KB for the sub-1-MiB chimes). */
function chimeSize(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return DASH;
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

/** Naive-local `YYYY-MM-DDThh:mm:ss` → a readable `YYYY-MM-DD hh:mm` (or "—"). */
function chimeModified(s: string | null | undefined): string {
  if (!s) return DASH;
  return s.replace("T", " ").slice(0, 16);
}

/** How a failed install/remove handoff should surface in the UI. */
interface ChimeFailure {
  message: string;
  /** Transient — keep the control live and offer a Retry. */
  retryable: boolean;
}

interface StagedChime {
  id: number;
  file: File;
  name: string;
  size: number;
  error: string | null | undefined;
}

interface ChimeUploadItem {
  id: number;
  name: string;
  size: number;
  status: "pending" | "uploading" | "done" | "error";
  error?: string;
}

/**
 * Map an install/remove rejection to operator-facing UI state, keyed on the HTTP
 * `status` (and `code` where it sharpens the message). Mirrors the contract's
 * §2.3.1 status map and the clip-delete classifier:
 *  - network / `409 handoff_busy` / `503 gadgetd_unavailable` → transient, retryable.
 *  - `400` / `422` (validation) → terminal.
 *  - `502 handoff_failed` / `500 critical_fault`|`staging_failed` → terminal fault.
 */
function classifyChimeFailure(err: unknown): ChimeFailure {
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
        message: "The device service is unavailable right now. Try again once it's back.",
        retryable: true,
      };
    }
    if (err.status === 400 || err.status === 422) {
      return { message: err.message, retryable: false };
    }
    if (err.status === 502) {
      return {
        message: `The chime change couldn't be completed on the car: ${err.message}`,
        retryable: false,
      };
    }
    if (err.status === 500) {
      return {
        message: `The device reported a fault during the chime change: ${err.message}`,
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

/** Validate the body of a PCM `fmt ` chunk client-side (mirrors webd's rules). */
function validateFmtChunk(dv: DataView, body: number, size: number): string | null {
  if (size < 16 || body + 16 > dv.byteLength) {
    return "That WAV's format header is too small to read.";
  }
  const audioFormat = dv.getUint16(body, true);
  const channels = dv.getUint16(body + 2, true);
  const sampleRate = dv.getUint32(body + 4, true);
  const byteRate = dv.getUint32(body + 8, true);
  const blockAlign = dv.getUint16(body + 12, true);
  const bits = dv.getUint16(body + 14, true);
  if (audioFormat !== 1) {
    return "Only PCM WAV lock chimes are supported — re-export as 16-bit PCM.";
  }
  if (channels !== 1 && channels !== 2) {
    return "The lock chime must be mono or stereo.";
  }
  if (sampleRate !== 44100 && sampleRate !== 48000) {
    return "The lock chime sample rate must be 44.1 or 48 kHz.";
  }
  if (bits !== 16) {
    return "The lock chime must be 16-bit PCM.";
  }
  const expectedBlock = channels * (bits / 8);
  if (blockAlign !== expectedBlock) {
    return "That WAV's header is inconsistent (block align vs. channels/bits).";
  }
  if (byteRate !== sampleRate * expectedBlock) {
    return "That WAV's header is inconsistent (byte rate vs. rate/channels/bits).";
  }
  return null;
}

/**
 * Best-effort client-side WAV validation so the operator gets instant feedback
 * before the eject-handoff runs. Faithfully mirrors webd's `validate_lock_chime_wav`
 * (RIFF/WAVE container, a PCM `fmt ` chunk, a non-empty `data` chunk, ≤1 MiB).
 * webd remains the authority; this only avoids a doomed handoff. Returns an
 * operator-facing message, or `null` when the file passes.
 */
async function validateChimeWav(file: File): Promise<string | null> {
  if (file.size > CHIME_MAX_BYTES) {
    return `That chime is ${chimeSize(file.size)} — lock chimes must be under 1 MB.`;
  }
  let buf: ArrayBuffer;
  try {
    buf = await file.arrayBuffer();
  } catch {
    return "That file couldn't be read. Try choosing it again.";
  }
  const dv = new DataView(buf);
  if (dv.byteLength < 12) return "That file is too small to be a WAV.";
  const tag = (off: number) =>
    String.fromCharCode(
      dv.getUint8(off),
      dv.getUint8(off + 1),
      dv.getUint8(off + 2),
      dv.getUint8(off + 3),
    );
  if (tag(0) !== "RIFF") return "That file isn't a WAV (missing RIFF header).";
  if (tag(8) !== "WAVE") return "That file isn't a WAV (missing WAVE form type).";

  let off = 12;
  let fmtSeen = false;
  let fmtErr: string | null = null;
  let dataNonEmpty = false;
  while (off + 8 <= dv.byteLength) {
    const id = tag(off);
    const chunkSize = dv.getUint32(off + 4, true);
    const bodyAt = off + 8;
    if (id === "fmt ") {
      fmtErr = validateFmtChunk(dv, bodyAt, chunkSize);
      fmtSeen = true;
    } else if (id === "data") {
      if (chunkSize > 0) dataNonEmpty = true;
    }
    // RIFF chunks are word-aligned: a pad byte follows an odd-length body.
    off = bodyAt + chunkSize + (chunkSize % 2);
  }
  if (fmtErr) return fmtErr;
  if (!fmtSeen) return "That WAV is missing its format (fmt) chunk.";
  if (!dataNonEmpty) return "That WAV has no audio data.";
  return null;
}

/** Fetch lifecycle of `GET /api/chimes`. */
type Status = "loading" | "ready" | "error";

export function Media() {
  useFullWidthScreen();
  const [status, setStatus] = useState<Status>("loading");
  const [installed, setInstalled] = useState<InstalledChime | null>(null);

  // ── Upload (install) state ──
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [staged, setStaged] = useState<StagedChime[]>([]);
  const [editorFile, setEditorFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [childBusy, setChildBusy] = useState(false);
  const [uploadItems, setUploadItems] = useState<ChimeUploadItem[]>([]);
  const [uploadProgress, setUploadProgress] = useState<{
    current: number;
    total: number;
  } | null>(null);
  const [uploadFail, setUploadFail] = useState<ChimeFailure | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pendingUpload, setPendingUpload] = useState<{
    filename: string;
    bytes: number;
    token: number;
  } | null>(null);
  const [pendingActivation, setPendingActivation] = useState<{
    filename: string;
    bytes: number;
    token: number;
    preModified: string | null;
    preSize: number | null;
    phase: "syncing" | "waiting";
  } | null>(null);
  const [activationNotice, setActivationNotice] = useState<string | null>(null);
  const [reenumOverlay, setReenumOverlay] = useState<{
    filename: string;
    token: number;
    phase: "syncing" | "waiting";
  } | null>(null);
  const [reenumPoll, setReenumPoll] = useState<{
    filename: string;
    token: number;
  } | null>(null);
  // The chime library (reported up by the embedded ChimeScheduler) + the chime
  // activated this session, used only to resolve the active card's source name.
  const [library, setLibrary] = useState<LibraryEntry[]>([]);
  const [lastActivated, setLastActivated] = useState<{
    filename: string;
    bytes: number;
  } | null>(null);

  const nextStagedIdRef = useRef(0);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const activationAbortRef = useRef<AbortController | null>(null);
  const reenumAbortRef = useRef<AbortController | null>(null);
  const reenumStopRef = useRef<(() => void) | null>(null);
  const reenumSawPendingRef = useRef(false);
  const latestActivationTokenRef = useRef<number>(0);
  // The reenum overlay is the single-overlay arbiter at the render boundary
  // (`show={showBusy && !reenumOverlay}`), so pageBusy intentionally does NOT
  // exclude reenumOverlay here: keeping pageBusy true across the reenum phase
  // means the 1s debounce timer doesn't reset, so the busy overlay reappears
  // instantly if activation is still syncing after reenum clears.
  const pageBusy =
    uploading ||
    childBusy ||
    (!!pendingActivation && pendingActivation.phase !== "waiting");
  const showBusy = useDelayedFlag(pageBusy, 1000);
  // Lock document scroll (and reserve the scrollbar gutter so the page doesn't
  // jump) whenever a full-screen overlay is up — either the busy blocker or the
  // reenum "keep doors closed" overlay. This stops the page being scrolled
  // behind the overlay and lets the fixed `inset:0` backdrop cover the viewport
  // edge-to-edge; otherwise the root scrollbar leaves an uncovered strip at the
  // right edge. Mirrors the app's existing html+body scroll-lock convention
  // (mapping-active). useLayoutEffect applies before paint so the first overlay
  // frame never flashes the scrollbar strip. NOTE: a window resize while locked
  // can leave the padding compensation marginally stale, but the overlay is
  // transient (a few seconds during an activation) and overflow:hidden still
  // removes the scrollbar regardless, so this is an accepted cosmetic trade-off.
  const anyOverlayOpen = pageBusy || !!reenumOverlay;
  useLayoutEffect(() => {
    if (!anyOverlayOpen) return;
    const { body } = document;
    const html = document.documentElement;
    const scrollbarWidth = window.innerWidth - html.clientWidth;
    const prevInlinePaddingRight = body.style.paddingRight;
    if (scrollbarWidth > 0) {
      const basePaddingRight = parseFloat(getComputedStyle(body).paddingRight) || 0;
      body.style.paddingRight = `${basePaddingRight + scrollbarWidth}px`;
    }
    html.classList.add("media-overlay-lock");
    body.classList.add("media-overlay-lock");
    return () => {
      html.classList.remove("media-overlay-lock");
      body.classList.remove("media-overlay-lock");
      body.style.paddingRight = prevInlinePaddingRight;
    };
  }, [anyOverlayOpen]);
  // Records the activation token whose reenum has already cleared, so the
  // independent chime-convergence poll can't clobber the "next lock" notice with
  // the plain success copy when reenum finishes before convergence is observed.
  const reenumDoneTokenRef = useRef<number>(0);
  // Monotonic activation sequence: every activation gets a globally unique token
  // so the poll effects (keyed on token) always restart + clean up the prior
  // poll. Deriving the token from the previous `pendingActivation` would reset to
  // 1 at rest, letting a still-running earlier reenum poll share a token with a
  // later activation and mislabel its "next lock" notice.
  const activationSeqRef = useRef(0);

  /** Reload `GET /api/chimes` after a successful mutation (or initial mount). */
  const refetch = (signal?: AbortSignal) =>
    api
      .chimes(signal)
      .then((c: Chimes) => {
        setInstalled(c.installed);
        setStatus("ready");
      })
      .catch(() => {
        if (!signal?.aborted) setStatus("error");
      });

  useEffect(() => {
    // Wiring-proof hook: prove THIS module produced the live DOM (defends the
    // documented "edited JS the page never loaded" failure mode).
    (
      window as unknown as {
        __TESLAUSB_MEDIA_HOOKS__?: { build: string; screen: string };
      }
    ).__TESLAUSB_MEDIA_HOOKS__ = { build: buildId(), screen: "lock-chimes" };

    const ctrl = new AbortController();
    void refetch(ctrl.signal);
    return () => {
      ctrl.abort();
      uploadAbortRef.current?.abort();
      activationAbortRef.current?.abort();
      reenumAbortRef.current?.abort();
      reenumStopRef.current?.();
    };
  }, []);

  async function stageFiles(incoming: File[]) {
    if (incoming.length === 0) return;
    setUploadFail(null);
    setNotice(null);
    const additions: StagedChime[] = incoming.map((file) => ({
      id: nextStagedIdRef.current++,
      file,
      name: file.name,
      size: file.size,
      error: undefined,
    }));
    setStaged((prev) => {
      const seen = new Set(prev.map((item) => `${item.name}:${item.size}`));
      const next = [...prev];
      for (const item of additions) {
        const key = `${item.name}:${item.size}`;
        if (seen.has(key)) continue;
        seen.add(key);
        next.push(item);
      }
      return next;
    });

    for (const item of additions) {
      const error = await validateChimeWav(item.file);
      setStaged((prev) =>
        prev.map((row) => (row.id === item.id ? { ...row, error } : row)),
      );
    }
  }

  async function handleSelectedFiles(incoming: File[]) {
    if (incoming.length === 0) return;
    setUploadFail(null);
    setNotice(null);
    setUploadItems([]);
    setUploadProgress(null);
    if (incoming.length === 1) {
      setEditorFile(incoming[0]);
      setStaged([]);
      return;
    }
    setEditorFile(null);
    await stageFiles(incoming);
  }

  async function onFileSelected(e: Event) {
    const input = e.currentTarget as HTMLInputElement;
    const files = input.files ? Array.from(input.files) : [];
    await handleSelectedFiles(files);
    input.value = "";
  }

  function removeStagedFile(id: number) {
    if (uploading) return;
    setStaged((prev) => prev.filter((item) => item.id !== id));
  }

  const chimeDrop = useFileDrop(
    (files) => {
      void handleSelectedFiles(files);
    },
    { disabled: uploading },
  );

  async function onUploadSubmit(e: Event) {
    e.preventDefault();
    const valid = staged.filter((item) => item.error === null);
    if (valid.length === 0 || uploading) return;
    setUploading(true);
    setUploadFail(null);
    setNotice(null);
    setUploadItems(
      valid.map((item) => ({
        id: item.id,
        name: item.name,
        size: item.size,
        status: "pending" as const,
      })),
    );
    setUploadProgress({ current: 0, total: valid.length });
    const ac = new AbortController();
    uploadAbortRef.current = ac;
    const succeeded: { name: string; bytes: number }[] = [];
    const failed: { id: number; name: string; fail: ChimeFailure }[] = [];
    for (let i = 0; i < valid.length; i++) {
      if (ac.signal.aborted) break;
      const item = valid[i];
      setUploadItems((prev) =>
        prev.map((entry) =>
          entry.id === item.id ? { ...entry, status: "uploading" } : entry,
        ),
      );
      setUploadProgress({ current: i + 1, total: valid.length });
      try {
        await api.uploadLibraryChime(item.file, ac.signal);
        succeeded.push({ name: catalogChimeName(item.name), bytes: item.size });
        setUploadItems((prev) =>
          prev.map((entry) => (entry.id === item.id ? { ...entry, status: "done" } : entry)),
        );
      } catch (err) {
        if (ac.signal.aborted) break;
        const fail = classifyChimeFailure(err);
        failed.push({ id: item.id, name: item.name, fail });
        setUploadItems((prev) =>
          prev.map((entry) =>
            entry.id === item.id
              ? { ...entry, status: "error", error: fail.message }
              : entry,
          ),
        );
      }
    }
    if (uploadAbortRef.current === ac) uploadAbortRef.current = null;
    if (ac.signal.aborted) {
      setUploading(false);
      return;
    }
    const failedIds = new Set(failed.map((entry) => entry.id));
    // Keep rows that failed (for retry) AND any row we never attempted — files
    // still validating or invalid at submit time must not be silently dropped.
    const attemptedIds = new Set(valid.map((item) => item.id));
    setStaged((prev) =>
      prev.filter((item) => failedIds.has(item.id) || !attemptedIds.has(item.id)),
    );
    if (fileInputRef.current) fileInputRef.current.value = "";

    if (succeeded.length > 0) {
      const last = succeeded[succeeded.length - 1];
      if (succeeded.length === 1) {
        applySingleUploadSuccess(last.name, last.bytes, setPendingUpload, setNotice);
      } else {
        setPendingUpload((prev) => ({
          filename: last.name,
          bytes: last.bytes,
          token: (prev?.token ?? 0) + 1,
        }));
        setNotice(`Upload accepted — syncing ${succeeded.length} chimes to your chime library below…`);
      }
    }
    if (failed.length > 0) {
      setUploadFail({
        message:
          failed.length === 1
            ? failed[0].fail.message
            : `${failed.length} file(s) failed to upload. ${failed[0].fail.message}`,
        retryable: failed.some((entry) => entry.fail.retryable),
      });
    } else {
      setUploadItems([]);
    }
    setUploadProgress(null);
    setUploading(false);
  }

  async function handleEditorUpload(uploadFile: File) {
    setUploading(true);
    setUploadFail(null);
    setNotice(null);
    const ac = new AbortController();
    uploadAbortRef.current = ac;
    try {
      await api.uploadLibraryChime(uploadFile, ac.signal);
      applySingleUploadSuccess(catalogChimeName(uploadFile.name), uploadFile.size, setPendingUpload, setNotice);
      setEditorFile(null);
      setUploadItems([]);
      setUploadProgress(null);
    } catch (err) {
      if (!ac.signal.aborted) {
        const fail = classifyChimeFailure(err);
        setUploadFail(fail);
        const editorError = new Error(fail.message) as Error & {
          retryable?: boolean;
        };
        editorError.retryable = fail.retryable;
        throw editorError;
      }
      throw err;
    } finally {
      if (uploadAbortRef.current === ac) uploadAbortRef.current = null;
      setUploading(false);
    }
  }

  /** Pick the activation notice: the "next lock" copy once reenum has cleared
   * for this token, otherwise the plain success copy. */
  function activationNoticeForToken(filename: string, token: number): string {
    return reenumDoneTokenRef.current === token
      ? activationNoticeNextLock(filename)
      : activationSuccessMessage(filename);
  }

  function onChimeActivated(filename: string, bytes: number) {
    setActivationNotice(null);
    const token = (activationSeqRef.current += 1);
    latestActivationTokenRef.current = token;
    setPendingActivation({
      filename,
      bytes,
      token,
      preModified: installed?.modified ?? null,
      preSize: installed?.size_bytes ?? null,
      phase: "syncing",
    });
  }

  async function refreshActivationNow() {
    if (!pendingActivation) return;
    try {
      const c = await api.chimes();
      setInstalled(c.installed);
      if (activationConverged(c.installed, pendingActivation)) {
        setLastActivated({ filename: pendingActivation.filename, bytes: pendingActivation.bytes });
        setActivationNotice(
          activationNoticeForToken(pendingActivation.filename, pendingActivation.token),
        );
        setPendingActivation(null);
      }
    } catch {
      // keep current waiting state when the catalog is unavailable.
    }
  }

  useEffect(() => {
    if (!pendingActivation?.token) return;

    let cancelled = false;
    const ctrl = new AbortController();
    activationAbortRef.current = ctrl;
    let pollId: ReturnType<typeof setTimeout> | null = null;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    const startedAt = Date.now();

    const stopPolling = () => {
      if (pollId) clearTimeout(pollId);
      if (timeoutId) clearTimeout(timeoutId);
      pollId = null;
      timeoutId = null;
    };

    const runPoll = async () => {
      if (cancelled) return;
      try {
        const c = await api.chimes(ctrl.signal);
        if (cancelled) return;
        setInstalled(c.installed);
        if (activationConverged(c.installed, pendingActivation)) {
          stopPolling();
          setLastActivated({ filename: pendingActivation.filename, bytes: pendingActivation.bytes });
          setActivationNotice(
            activationNoticeForToken(pendingActivation.filename, pendingActivation.token),
          );
          setPendingActivation(null);
          return;
        }
      } catch {
        // Aborted (unmount/new-token/timeout) or a transient read failure: fall
        // through to re-arm so a momentary blip can't silently stop the poll.
        if (cancelled) return;
      }
      if (Date.now() - startedAt < ACT_POLL_MAX_MS) {
        pollId = setTimeout(() => {
          void runPoll();
        }, ACT_POLL_INTERVAL_MS);
      }
    };

    setPendingActivation((current) =>
      current && current.token === pendingActivation.token
        ? { ...current, phase: "syncing" }
        : current,
    );

    void runPoll();
    timeoutId = setTimeout(() => {
      if (cancelled) return;
      ctrl.abort();
      stopPolling();
      setPendingActivation((current) =>
        current && current.token === pendingActivation.token
          ? { ...current, phase: "waiting" }
          : current,
      );
    }, ACT_POLL_MAX_MS);

    return () => {
      cancelled = true;
      ctrl.abort();
      stopPolling();
      if (activationAbortRef.current === ctrl) activationAbortRef.current = null;
    };
  }, [pendingActivation?.token]);

  useEffect(() => {
    if (!pendingActivation?.token) return;
    reenumSawPendingRef.current = false;
    setReenumOverlay(null);
    setReenumPoll({ filename: pendingActivation.filename, token: pendingActivation.token });
  }, [pendingActivation?.token]);

  useEffect(() => {
    if (!reenumPoll?.token) return;

    let cancelled = false;
    const ctrl = new AbortController();
    reenumAbortRef.current = ctrl;
    let pollId: ReturnType<typeof setTimeout> | null = null;
    let syncingId: ReturnType<typeof setTimeout> | null = null;
    let maxId: ReturnType<typeof setTimeout> | null = null;

    const stopPolling = () => {
      if (pollId) clearTimeout(pollId);
      if (syncingId) clearTimeout(syncingId);
      if (maxId) clearTimeout(maxId);
      pollId = null;
      syncingId = null;
      maxId = null;
    };
    reenumStopRef.current = stopPolling;

    const runPoll = async () => {
      if (cancelled || ctrl.signal.aborted) return;
      try {
        const status = await api.gadgetStatus(ctrl.signal);
        if (cancelled || ctrl.signal.aborted) return;
        // A newer activation has superseded this poll (its effect cleanup may not
        // have run yet): stop without touching overlay/notice state so a stale
        // poll can never own a later activation's UI.
        if (latestActivationTokenRef.current !== reenumPoll.token) {
          stopPolling();
          return;
        }
        if (status.chime_reenum_pending) {
          reenumSawPendingRef.current = true;
          setReenumOverlay((current) => {
            if (current && current.token === reenumPoll.token && current.phase === "waiting") {
              return current;
            }
            return { filename: reenumPoll.filename, token: reenumPoll.token, phase: "syncing" };
          });
        } else if (reenumSawPendingRef.current) {
          stopPolling();
          setReenumOverlay((current) =>
            current && current.token === reenumPoll.token ? null : current,
          );
          reenumDoneTokenRef.current = reenumPoll.token;
          setActivationNotice(activationNoticeNextLock(reenumPoll.filename));
          setReenumPoll((current) =>
            current && current.token === reenumPoll.token ? null : current,
          );
          return;
        }
      } catch {
        if (cancelled || ctrl.signal.aborted) return;
      }
      pollId = setTimeout(() => {
        void runPoll();
      }, REENUM_POLL_INTERVAL_MS);
    };

    void runPoll();
    syncingId = setTimeout(() => {
      if (cancelled || ctrl.signal.aborted) return;
      setReenumOverlay((current) =>
        current && current.token === reenumPoll.token
          ? { ...current, phase: "waiting" }
          : current,
      );
    }, REENUM_SYNCING_MAX_MS);

    maxId = setTimeout(() => {
      if (cancelled) return;
      ctrl.abort();
      stopPolling();
      setReenumOverlay((current) =>
        current && current.token === reenumPoll.token
          ? { ...current, phase: "waiting" }
          : current,
      );
      setReenumPoll((current) =>
        current && current.token === reenumPoll.token ? null : current,
      );
    }, REENUM_POLL_MAX_MS);

    return () => {
      cancelled = true;
      ctrl.abort();
      stopPolling();
      if (reenumAbortRef.current === ctrl) reenumAbortRef.current = null;
      if (reenumStopRef.current === stopPolling) reenumStopRef.current = null;
    };
  }, [reenumPoll?.token]);

  function dismissReenumOverlay() {
    if (!reenumOverlay) return;
    const token = reenumOverlay.token;
    // Only abort the live poll when the dismissed overlay actually belongs to it;
    // during a rapid re-activation the visible overlay can be a stale token, and
    // aborting then would kill the NEW activation's poll.
    if (reenumPoll?.token === token) {
      reenumAbortRef.current?.abort();
      reenumStopRef.current?.();
      setReenumPoll(null);
    }
    setReenumOverlay(null);
  }

  const validCount = staged.filter((item) => item.error === null).length;
  const validating = staged.some((item) => item.error === undefined);

  return (
    <>
      <div class="container media-page" data-page="media" data-screen="media">
      {/* ── Media pill sub-nav (v1 media_hub_nav.html parity) ── */}
      <MediaPills active="chimes" />

      <h2>Lock Chimes</h2>

      {/* ── Active Lock Chime ── (live from GET /api/chimes) */}
      <div class="media-card" id="activeChimeSection">
        <h3>Active Lock Chime</h3>
        {status === "ready" && installed ? (
          (() => {
            const sourceName = resolveActiveSourceName(installed, library, lastActivated);
            return (
              <div class="active-chime" data-testid="active-chime">
                <div class="active-chime-name" data-testid="active-chime-name">
                  {sourceName ?? installed.name}
                </div>
                {sourceName && (
                  <div class="active-chime-source" data-testid="active-chime-source">
                    Installed as {installed.name}
                  </div>
                )}
                <div class="active-chime-meta">
                  <span class="chime-pill">{chimeSize(installed.size_bytes)}</span>
                  <span class="chime-pill">
                    Installed {chimeModified(installed.modified)}
                  </span>
                </div>
                <audio
                  class="active-chime-player"
                  controls
                  preload="none"
                  data-testid="active-chime-audio"
                  key={installed.modified ?? String(installed.size_bytes)}
                  src={api.activeChimeAudioUrl(installed.modified)}
                />
              </div>
            );
          })()
        ) : status === "ready" ? (
          <p class="media-pending" data-testid="active-chime-none">
            No lock chime is installed. The vehicle will play its built-in chime
            until one is installed below.
          </p>
        ) : status === "error" ? (
          <p class="media-pending" data-testid="active-chime-error">
            The active lock chime couldn’t be read just now. It will appear here
            once the media catalog can be reached.
          </p>
        ) : (
          <p class="media-pending" data-testid="active-chime-loading">
            Reading the installed lock chime…
          </p>
        )}
        {/* Activation progress/result — rendered regardless of whether a chime
            is currently installed (so promoting the first-ever chime, or a poll
            that transiently reports {installed:null}, still shows status). */}
        {pendingActivation?.phase === "syncing" && (
          <p data-testid="activation-status">
            Applying “{pendingActivation.filename}” to the car — this usually takes about 15–30 seconds…
          </p>
        )}
        {pendingActivation?.phase === "waiting" && (
          <>
            <p data-testid="activation-status">
              Still applying “{pendingActivation.filename}” — it should appear shortly.
            </p>
            <button
              type="button"
              data-testid="activation-refresh-now"
              onClick={() => void refreshActivationNow()}
            >
              Refresh now
            </button>
          </>
        )}
        {activationNotice && !pendingActivation && (
          <p data-testid="activation-notice">{activationNotice}</p>
        )}
      </div>

      {/* ── Upload New Chime ── (operator-gated install: pick-a-file → Upload) */}
      <details class="settings-section" id="chimeUploadControls" open>
        <summary>Upload New Chime</summary>
        <div class="section-content">
          {editorFile ? (
            <ChimeAudioEditor
              file={editorFile}
              onUpload={handleEditorUpload}
              onCancel={() => {
                setEditorFile(null);
                setUploadFail(null);
                setNotice(null);
              }}
            />
          ) : (
            <form class="chime-upload" onSubmit={onUploadSubmit} novalidate>
            <div
              class={`chime-dropzone${chimeDrop.dragging ? " dragging" : ""}`}
              data-testid="chime-dropzone"
              {...chimeDrop.dropHandlers}
            >
              <p class="chime-dropzone-label">
                <Icon name="upload" class="chime-btn-icon" /> Drag &amp; drop a .wav here, or choose a file
              </p>
              <div class="chime-upload-row">
                <input
                  ref={fileInputRef}
                  type="file"
                  id="chime_file"
                  name="file"
                  class="chime-file-input"
                  accept="audio/*,.wav,.mp3,.m4a,.aac,.ogg,.flac"
                  data-testid="chime-file-input"
                  multiple
                  onChange={onFileSelected}
                  disabled={uploading}
                />
                <button
                  type="submit"
                  class="action-btn primary chime-upload-btn"
                  data-testid="chime-upload-submit"
                  disabled={validCount === 0 || uploading || validating}
                  aria-busy={uploading ? "true" : "false"}
                >
                  {uploading ? (
                    <>
                      <span class="chime-spinner" aria-hidden="true" /> Uploading
                      {"\u2026"}
                    </>
                  ) : (
                    <>
                      <Icon name="upload" class="chime-btn-icon" /> {validCount > 1 ? `Upload ${validCount} chimes` : "Upload"}
                    </>
                  )}
                </button>
              </div>
            </div>
            <p class="chime-upload-hint">
              Add one or more valid .wav files. Uploads run one by one and show
              progress below.
            </p>

            {!uploading && staged.length > 0 && (
              <ul class="chime-staged-list" data-testid="chime-upload-staged">
                {staged.map((item) => (
                  <li
                    class="chime-staged-row"
                    data-testid="chime-staged-row"
                    key={item.id}
                  >
                    <strong>{item.name}</strong>
                    <span>({chimeSize(item.size)})</span>
                    {typeof item.error === "string" ? (
                      <span class="chime-staged-error" role="alert" data-testid="chime-staged-error">
                        {item.error}
                      </span>
                    ) : (
                      <span class="chime-staged-ok">{item.error === undefined ? "checking…" : "ready"}</span>
                    )}
                    <button
                      type="button"
                      class="chime-staged-remove"
                      data-testid="chime-staged-remove"
                      aria-label={`Remove ${item.name}`}
                      onClick={() => removeStagedFile(item.id)}
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}

            {uploadItems.length > 0 && (
              <ul class="chime-upload-list" data-testid="chime-upload-progress-list">
                {uploadItems.map((item) => (
                  <li
                    class={`chime-upload-item status-${item.status}`}
                    data-testid="chime-upload-item"
                    key={item.id}
                  >
                    <span aria-hidden="true">
                      {item.status === "uploading"
                        ? "↻"
                        : item.status === "done"
                          ? "✓"
                          : item.status === "error"
                            ? "✗"
                            : "•"}
                    </span>
                    <span>{item.name}</span>
                    <span>({chimeSize(item.size)})</span>
                    {item.error && <span>{item.error}</span>}
                  </li>
                ))}
              </ul>
            )}

            {uploadProgress && (
              <p class="chime-upload-status" role="status" data-testid="chime-upload-progress">
                Uploading {uploadProgress.current}/{uploadProgress.total}…
              </p>
            )}

            {uploadFail && (
              <p
                class={`chime-upload-status ${uploadFail.retryable ? "retryable" : "fatal"}`}
                role="alert"
                data-testid="chime-upload-error"
              >
                {uploadFail.message}
              </p>
            )}

            {notice && (
              <p
                class="chime-upload-status success"
                role="status"
                data-testid="chime-notice"
              >
                {notice}
              </p>
            )}
          </form>
          )}
        </div>
      </details>

      {/* ── Chime Scheduler · Random Groups · Library ── (live: schedulerd via webd) */}
        <ChimeScheduler
          pendingUpload={pendingUpload}
          onActivated={onChimeActivated}
          onLibraryLoaded={setLibrary}
          activationBusy={!!pendingActivation}
          onBusyChange={setChildBusy}
        />
      </div>
      <BusyOverlay block={pageBusy && !reenumOverlay} visible={showBusy && !reenumOverlay} />
      {reenumOverlay && (
        <div
          class="media-page reenum-overlay-backdrop"
          data-testid="reenum-overlay"
          role="dialog"
          aria-modal="true"
        >
          <div class="reenum-overlay-card">
            <span class="reenum-overlay-spinner" aria-hidden="true" />
            <h3 class="reenum-overlay-title">Syncing chime to your car</h3>
            <p
              class="reenum-overlay-message"
              data-testid="reenum-overlay-message"
              aria-live="assertive"
            >
              {reenumOverlay.phase === "syncing"
                ? "Keep the car’s doors closed for a few seconds while the USB drive reconnects so your new lock chime takes effect."
                : "This is taking a little longer — it will finish once the car is idle. Keep the doors closed, or close them now to let it complete."}
            </p>
            <button
              type="button"
              class="action-btn reenum-overlay-dismiss"
              data-testid="reenum-overlay-dismiss"
              onClick={dismissReenumOverlay}
            >
              Dismiss
            </button>
          </div>
        </div>
      )}
    </>
  );
}
