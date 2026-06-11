import { useEffect, useRef, useState } from "preact/hooks";
import { MediaPills } from "../components/MediaPills";
import { Icon } from "../components/Icon";
import { ChimeScheduler } from "./ChimeScheduler";
import { api, ApiError, CHIME_MAX_BYTES, isQueued } from "../api/client";
import type { Chimes, InstalledChime } from "../api/types";
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

function buildId(): string {
  return (
    (window as unknown as { __TESLAUSB_BUILD__?: string }).__TESLAUSB_BUILD__ ??
    "dev"
  );
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
  const [status, setStatus] = useState<Status>("loading");
  const [installed, setInstalled] = useState<InstalledChime | null>(null);

  // ── Upload (install) state ──
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadFail, setUploadFail] = useState<ChimeFailure | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // ── Remove state ──
  const [removePending, setRemovePending] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removeFail, setRemoveFail] = useState<ChimeFailure | null>(null);

  const uploadAbortRef = useRef<AbortController | null>(null);
  const removeAbortRef = useRef<AbortController | null>(null);

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
      removeAbortRef.current?.abort();
    };
  }, []);

  async function onFileSelected(e: Event) {
    setUploadFail(null);
    setNotice(null);
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0] ?? null;
    if (!file) {
      setSelectedFile(null);
      setValidationError(null);
      return;
    }
    setSelectedFile(file);
    setValidationError(await validateChimeWav(file));
  }

  function resetUpload() {
    setSelectedFile(null);
    setValidationError(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function onUploadSubmit(e: Event) {
    e.preventDefault();
    if (!selectedFile || validationError || uploading) return;
    setUploading(true);
    setUploadFail(null);
    setNotice(null);
    const ac = new AbortController();
    uploadAbortRef.current = ac;
    try {
      const res = await api.installChime(selectedFile, ac.signal);
      const name = selectedFile.name;
      resetUpload();
      setNotice(
        isQueued(res)
          ? `Saved ${name} as the active lock chime — syncing to the car.`
          : `Installed ${name} as the active lock chime.`,
      );
      await refetch();
    } catch (err) {
      if (ac.signal.aborted) return; // silent: user/unmount cancelled
      setUploadFail(classifyChimeFailure(err));
    } finally {
      if (uploadAbortRef.current === ac) uploadAbortRef.current = null;
      setUploading(false);
    }
  }

  function openRemove() {
    setRemoveFail(null);
    setRemovePending(true);
  }

  function closeRemove() {
    if (removing) return;
    setRemovePending(false);
    setRemoveFail(null);
  }

  async function confirmRemove() {
    if (removing) return;
    setRemoving(true);
    setRemoveFail(null);
    const ac = new AbortController();
    removeAbortRef.current = ac;
    try {
      const res = await api.removeChime(ac.signal);
      setRemovePending(false);
      setNotice(
        isQueued(res)
          ? "Removing the lock chime — syncing to the car."
          : "Removed the lock chime — the car will use its built-in chime.",
      );
      await refetch();
    } catch (err) {
      if (ac.signal.aborted) return; // silent: user/unmount cancelled
      setRemoveFail(classifyChimeFailure(err));
    } finally {
      if (removeAbortRef.current === ac) removeAbortRef.current = null;
      setRemoving(false);
    }
  }

  return (
    <div class="container media-page" data-page="media" data-screen="media">
      {/* ── Media pill sub-nav (v1 media_hub_nav.html parity) ── */}
      <MediaPills active="chimes" />

      <h2>Lock Chimes</h2>

      {/* ── Active Lock Chime ── (live from GET /api/chimes) */}
      <div class="media-card" id="activeChimeSection">
        <h3>Active Lock Chime</h3>
        {status === "ready" && installed ? (
          <div class="active-chime" data-testid="active-chime">
            <div class="active-chime-name" data-testid="active-chime-name">
              {installed.name}
            </div>
            <div class="active-chime-meta">
              <span class="chime-pill">{chimeSize(installed.size_bytes)}</span>
              <span class="chime-pill">
                Installed {chimeModified(installed.modified)}
              </span>
            </div>
            <div class="active-chime-actions">
              <button
                type="button"
                class="action-btn danger chime-remove-btn"
                data-testid="active-chime-remove"
                onClick={openRemove}
                disabled={removing}
              >
                <Icon name="trash-2" class="chime-btn-icon" />
                Remove
              </button>
            </div>
          </div>
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
      </div>

      {/* ── Upload New Chime ── (operator-gated install: pick-a-file → Upload) */}
      <details class="settings-section" id="chimeUploadControls" open>
        <summary>Upload New Chime</summary>
        <div class="section-content">
          <form class="chime-upload" onSubmit={onUploadSubmit} novalidate>
            <div class="chime-upload-row">
              <input
                ref={fileInputRef}
                type="file"
                id="chime_file"
                name="file"
                class="chime-file-input"
                accept=".wav,audio/wav,audio/x-wav,audio/wave,audio/vnd.wave"
                data-testid="chime-file-input"
                onChange={onFileSelected}
                disabled={uploading}
              />
              <button
                type="submit"
                class="action-btn primary chime-upload-btn"
                data-testid="chime-upload-submit"
                disabled={!selectedFile || !!validationError || uploading}
                aria-busy={uploading ? "true" : "false"}
              >
                {uploading ? (
                  <>
                    <span class="chime-spinner" aria-hidden="true" /> Installing
                    {"\u2026"}
                  </>
                ) : (
                  <>
                    <Icon name="upload" class="chime-btn-icon" /> Upload
                  </>
                )}
              </button>
            </div>
            <p class="chime-upload-hint">
              A finished 16-bit PCM WAV — mono or stereo, 44.1 or 48&nbsp;kHz,
              under 1&nbsp;MB. Installing replaces the active chime and briefly
              ejects the USB drive from the vehicle.
            </p>

            {selectedFile && !validationError && !uploading && !uploadFail && (
              <p
                class="chime-upload-selected"
                data-testid="chime-upload-selected"
              >
                Ready to install <strong>{selectedFile.name}</strong> (
                {chimeSize(selectedFile.size)}).
              </p>
            )}

            {validationError && (
              <p
                class="chime-upload-status fatal"
                role="alert"
                data-testid="chime-upload-validation"
              >
                {validationError}
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
        </div>
      </details>

      {/* ── Chime Scheduler · Random Groups · Library ── (live: schedulerd via webd) */}
      <ChimeScheduler />

      {/* ── Operator-gated remove confirmation (names the chime; no one-click). ── */}
      {removePending && (
        <div
          class="chime-modal-backdrop"
          role="presentation"
          onClick={closeRemove}
        >
          <div
            class="chime-modal"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="chimeRemoveTitle"
            aria-describedby="chimeRemoveDesc"
            data-testid="chime-remove-dialog"
            onClick={(e: Event) => e.stopPropagation()}
          >
            <h3 id="chimeRemoveTitle" class="chime-modal-title">
              Remove the lock chime?
            </h3>
            <p id="chimeRemoveDesc" class="chime-modal-desc">
              This removes{" "}
              <strong class="chime-modal-name">
                {installed?.name ?? "LockChime.wav"}
              </strong>{" "}
              from the vehicle and briefly ejects the USB drive. The car returns
              to its built-in chime.
            </p>

            {removeFail && (
              <div
                class={`chime-modal-status${removeFail.retryable ? " retryable" : " fatal"}`}
                role="alert"
                data-testid="chime-remove-error"
              >
                {removeFail.message}
              </div>
            )}

            <div class="chime-modal-actions">
              <button
                type="button"
                class="chime-modal-btn cancel"
                onClick={closeRemove}
                disabled={removing}
              >
                {removeFail && !removeFail.retryable ? "Close" : "Cancel"}
              </button>
              {(!removeFail || removeFail.retryable) && (
                <button
                  type="button"
                  class="chime-modal-btn confirm"
                  data-testid="chime-remove-confirm"
                  onClick={confirmRemove}
                  disabled={removing}
                  aria-busy={removing ? "true" : "false"}
                >
                  {removing ? (
                    <>
                      <span class="chime-spinner" aria-hidden="true" /> Removing
                      {"\u2026"}
                    </>
                  ) : removeFail ? (
                    "Retry"
                  ) : (
                    "Remove"
                  )}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
