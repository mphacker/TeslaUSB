import { useCallback, useEffect, useMemo, useRef, useState } from "preact/hooks";
import {
  ChimeAudioEngine,
  WebAudioUnavailableError,
  type DecodedAudio,
} from "../audio/chime-engine";
import {
  CHIME_MAX_BYTES,
  CHIME_TARGET_SAMPLE_RATE,
  encodeWavWithClipCount,
  maxFrames,
  validateWav,
} from "../audio/wav-core";

const CHIME_MAX_DURATION_SECONDS = 10;
const CHIME_MIN_DURATION_SECONDS = 0.3;
const MAX_SOURCE_BYTES = 30 * 1024 * 1024;

const LOUDNESS_PRESETS = [
  {
    lufs: -23,
    name: "Broadcast",
    description: "Broadcast standard (quieter, more headroom)",
  },
  {
    lufs: -16,
    name: "Streaming",
    description: "Recommended for balanced playback",
  },
  {
    lufs: -14,
    name: "Loud",
    description: "Louder output (less dynamic range)",
  },
  {
    lufs: -12,
    name: "Maximum",
    description: "Maximum loudness (may clip on some systems)",
  },
] as const;

const NON_PROCESSABLE_AUDIO_MESSAGE =
  "This browser can't process this audio — please upload a 16-bit PCM WAV.";

const OVERSIZE_RAW_WAV_MESSAGE =
  "This browser can't process this audio, and the original WAV is over 1 MiB so it can't be uploaded as-is. Please trim it to 1 MiB or smaller first.";

function isRetryableError(cause: unknown): boolean {
  return !!(
    cause &&
    typeof cause === "object" &&
    "retryable" in cause &&
    (cause as { retryable?: unknown }).retryable === true
  );
}

type EditorMode = "loading" | "ready" | "fallback" | "error";

interface ChimeAudioEditorProps {
  file: File;
  onUpload: (file: File) => Promise<void>;
  onCancel: () => void;
}

function toSecondsText(seconds: number): string {
  return `${Math.max(0, seconds).toFixed(2)}s`;
}

function toBytesText(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}

function sanitizeInitialFilename(name: string): string {
  const withoutExt = name.replace(/\.wav$/i, "").replace(/\.[^.]+$/, "");
  const safe = withoutExt.replace(/[^A-Za-z0-9 _.-]/g, "_").trim();
  return safe.length ? safe : "lock-chime";
}

function normalizeBaseFilename(input: string): string {
  const trimmed = input.trim();
  return trimmed.replace(/\.wav$/i, "").trim();
}

function validateBaseFilename(baseName: string): string | null {
  if (!baseName) return "Please enter an output filename.";
  if (baseName === "." || baseName === "..") return "The output filename is invalid.";
  if (baseName.includes("/") || baseName.includes("\\")) {
    return "Path separators are not allowed in the output filename.";
  }
  if (baseName.includes("\0")) return "The output filename contains invalid characters.";
  if (!/^[A-Za-z0-9 _.-]+$/.test(baseName)) {
    return "Use only letters, numbers, spaces, underscore (_), hyphen (-), or period (.).";
  }
  const byteLength = new TextEncoder().encode(baseName).length;
  if (byteLength > 255) return "The output filename must be 255 bytes or fewer.";
  return null;
}

function outputChannelCount(decoded: DecodedAudio | null): number {
  if (!decoded) return 1;
  return decoded.channels.length > 2 ? 1 : Math.max(1, decoded.channels.length);
}

export function ChimeAudioEditor({ file, onUpload, onCancel }: ChimeAudioEditorProps) {
  const engineRef = useRef<ChimeAudioEngine | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const loadTokenRef = useRef(0);

  const [mode, setMode] = useState<EditorMode>("loading");
  const [error, setError] = useState<string | null>(null);
  const [errorRetryable, setErrorRetryable] = useState(false);
  const [decoded, setDecoded] = useState<DecodedAudio | null>(null);
  const [startTime, setStartTime] = useState(0);
  const [endTime, setEndTime] = useState(0);
  const [normalize, setNormalize] = useState(true);
  const [presetIndex, setPresetIndex] = useState(1);
  const [filename, setFilename] = useState(sanitizeInitialFilename(file.name));
  const [playhead, setPlayhead] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [estimatedSize, setEstimatedSize] = useState(44);
  const [actualSize, setActualSize] = useState(44);
  const [clipCount, setClipCount] = useState(0);
  const [silentNormalizationSkipped, setSilentNormalizationSkipped] =
    useState(false);
  const [rawWavUploadAllowed, setRawWavUploadAllowed] = useState(false);

  const duration = decoded?.duration ?? 0;
  const channels = outputChannelCount(decoded);
  const preset = LOUDNESS_PRESETS[presetIndex] ?? LOUDNESS_PRESETS[1];
  const selectedFrames = decoded
    ? Math.max(
        0,
        Math.floor(endTime * decoded.sampleRate) -
          Math.floor(startTime * decoded.sampleRate),
      )
    : 0;
  const overLimit =
    44 + selectedFrames * channels * 2 > CHIME_MAX_BYTES ||
    Math.max(actualSize, estimatedSize) > CHIME_MAX_BYTES;

  const statusText = useMemo(() => {
    if (overLimit) return "Over limit";
    if (silentNormalizationSkipped) {
      return "Ready — silent/very quiet source, normalization skipped";
    }
    return "Ready";
  }, [overLimit, silentNormalizationSkipped]);

  const recalcEstimatedSize = useCallback(
    (nextStart: number, nextEnd: number, source: DecodedAudio | null) => {
      if (!source) return;
      const selectedFrames = Math.max(
        0,
        Math.floor(nextEnd * source.sampleRate) -
          Math.floor(nextStart * source.sampleRate),
      );
      setEstimatedSize(44 + selectedFrames * channels * 2);
    },
    [channels],
  );

  const computeActualForSelection = useCallback(
    (nextStart = startTime, nextEnd = endTime, source = decoded) => {
      if (!source || !engineRef.current) return;
      const processed = engineRef.current.extractProcessed({
        source,
        start: nextStart,
        end: nextEnd,
        normalize,
        targetLufs: preset.lufs,
        maxBytes: Number.MAX_SAFE_INTEGER,
      });
      const encoded = encodeWavWithClipCount(processed.pcm);
      setActualSize(encoded.buffer.byteLength);
      setClipCount(encoded.clipCount);
      setSilentNormalizationSkipped(processed.silentNormalizationSkipped);
    },
    [decoded, endTime, normalize, preset.lufs, startTime],
  );

  const stopPreview = useCallback(() => {
    engineRef.current?.stop();
    setIsPlaying(false);
    setPlayhead(startTime);
  }, [startTime]);

  useEffect(() => {
    const engine = new ChimeAudioEngine();
    engineRef.current = engine;
    return () => {
      engine.dispose();
      engineRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!engineRef.current) return;
    const engine = engineRef.current;
    const token = ++loadTokenRef.current;
    let canceled = false;
    setMode("loading");
    setError(null);
    setDecoded(null);
    setRawWavUploadAllowed(false);
    setNormalize(true);
    setPresetIndex(1);
    setFilename(sanitizeInitialFilename(file.name));
    setClipCount(0);
    setSilentNormalizationSkipped(false);
    setPlayhead(0);
    setUploading(false);

    const load = async () => {
      if (file.size > MAX_SOURCE_BYTES) {
        if (canceled || loadTokenRef.current !== token) return;
        setMode("error");
        setError(
          "This source file is too large for in-browser processing. Please choose a file under 30 MB.",
        );
        return;
      }

      try {
        const decodedAudio = await engine.decode(file);
        if (canceled || loadTokenRef.current !== token) return;

        const initialStart = 0;
        let initialEnd = Math.min(
          decodedAudio.duration,
          CHIME_MAX_DURATION_SECONDS,
        );
        const maxDurationBySize =
          maxFrames(outputChannelCount(decodedAudio)) / CHIME_TARGET_SAMPLE_RATE;
        initialEnd = Math.min(initialEnd, initialStart + maxDurationBySize);
        if (
          decodedAudio.duration >= CHIME_MIN_DURATION_SECONDS &&
          initialEnd - initialStart < CHIME_MIN_DURATION_SECONDS
        ) {
          initialEnd = Math.min(
            decodedAudio.duration,
            initialStart + CHIME_MIN_DURATION_SECONDS,
          );
        }

        setDecoded(decodedAudio);
        setStartTime(initialStart);
        setEndTime(initialEnd);
        setPlayhead(initialStart);
        setMode("ready");
        recalcEstimatedSize(initialStart, initialEnd, decodedAudio);
        computeActualForSelection(initialStart, initialEnd, decodedAudio);
      } catch (cause) {
        if (canceled || loadTokenRef.current !== token) return;
        try {
          const bytes = new Uint8Array(await file.arrayBuffer());
          if (canceled || loadTokenRef.current !== token) return;
          const validation = validateWav(bytes);
          if (
            validation.ok &&
            (cause instanceof WebAudioUnavailableError || cause instanceof Error)
          ) {
            if (file.size > CHIME_MAX_BYTES) {
              setMode("error");
              setError(OVERSIZE_RAW_WAV_MESSAGE);
              return;
            }
            setMode("fallback");
            setRawWavUploadAllowed(true);
            setActualSize(file.size);
            setEstimatedSize(file.size);
            return;
          }
        } catch {
          // fall through to error state
        }
        setMode("error");
        setError(NON_PROCESSABLE_AUDIO_MESSAGE);
      }
    };

    void load();
    return () => {
      canceled = true;
      engine.stop();
    };
  }, [file]);

  useEffect(() => {
    if (mode !== "ready" || !decoded) return;
    recalcEstimatedSize(startTime, endTime, decoded);
  }, [decoded, endTime, mode, recalcEstimatedSize, startTime]);

  useEffect(() => {
    if (mode !== "ready" || !decoded) return;
    computeActualForSelection(startTime, endTime, decoded);
  }, [
    computeActualForSelection,
    decoded,
    endTime,
    mode,
    normalize,
    presetIndex,
    startTime,
  ]);

  useEffect(() => {
    if (!decoded || !canvasRef.current || mode !== "ready" || !engineRef.current) {
      return;
    }
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const cssWidth = Math.max(1, canvas.clientWidth);
    const cssHeight = Math.max(1, canvas.clientHeight);
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(cssWidth * dpr);
    canvas.height = Math.floor(cssHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    const peaks = engineRef.current.waveformPeaks(decoded.channels, 200);
    const midY = cssHeight / 2;
    const barWidth = cssWidth / peaks.length;
    const startX = (startTime / decoded.duration) * cssWidth;
    const endX = (endTime / decoded.duration) * cssWidth;

    ctx.fillStyle = "rgba(127,127,127,0.18)";
    ctx.fillRect(0, 0, cssWidth, cssHeight);
    ctx.fillStyle = "rgba(33, 150, 243, 0.22)";
    ctx.fillRect(startX, 0, Math.max(2, endX - startX), cssHeight);
    ctx.strokeStyle = "rgba(255,255,255,0.55)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i < peaks.length; i++) {
      const x = i * barWidth + barWidth / 2;
      const amp = Math.max(0.01, peaks[i]);
      const height = Math.max(2, amp * cssHeight * 0.9);
      ctx.moveTo(x, midY - height / 2);
      ctx.lineTo(x, midY + height / 2);
    }
    ctx.stroke();
    ctx.strokeStyle = "rgba(255, 87, 34, 0.9)";
    ctx.beginPath();
    ctx.moveTo(startX, 0);
    ctx.lineTo(startX, cssHeight);
    ctx.moveTo(endX, 0);
    ctx.lineTo(endX, cssHeight);
    ctx.stroke();

    const playheadX = (playhead / decoded.duration) * cssWidth;
    ctx.strokeStyle = "rgba(255, 193, 7, 0.9)";
    ctx.beginPath();
    ctx.moveTo(playheadX, 0);
    ctx.lineTo(playheadX, cssHeight);
    ctx.stroke();
  }, [decoded, endTime, mode, playhead, startTime]);

  const onStartChange = useCallback(
    (next: number) => {
      const maxStart = Math.max(
        0,
        endTime - Math.min(CHIME_MIN_DURATION_SECONDS, duration),
      );
      const clamped = Math.min(Math.max(0, next), maxStart);
      setStartTime(clamped);
      if (playhead < clamped) setPlayhead(clamped);
      setActualSize(estimatedSize);
      recalcEstimatedSize(clamped, endTime, decoded);
    },
    [decoded, duration, endTime, estimatedSize, playhead, recalcEstimatedSize],
  );

  const onEndChange = useCallback(
    (next: number) => {
      const minEnd = Math.min(
        duration,
        startTime + Math.min(CHIME_MIN_DURATION_SECONDS, duration),
      );
      const clamped = Math.max(Math.min(duration, next), minEnd);
      setEndTime(clamped);
      if (playhead > clamped) setPlayhead(clamped);
      setActualSize(estimatedSize);
      recalcEstimatedSize(startTime, clamped, decoded);
    },
    [decoded, duration, estimatedSize, playhead, recalcEstimatedSize, startTime],
  );

  const onAutoFit = useCallback(() => {
    if (!decoded) return;
    const maxDurationByBytes = maxFrames(channels) / CHIME_TARGET_SAMPLE_RATE;
    const trim = endTime - startTime;
    let nextEnd = startTime + Math.min(trim, maxDurationByBytes);
    if (
      decoded.duration >= CHIME_MIN_DURATION_SECONDS &&
      nextEnd - startTime < CHIME_MIN_DURATION_SECONDS
    ) {
      nextEnd = Math.min(decoded.duration, startTime + CHIME_MIN_DURATION_SECONDS);
    }
    nextEnd = Math.min(decoded.duration, nextEnd);
    setEndTime(nextEnd);
    setPlayhead(Math.max(startTime, Math.min(playhead, nextEnd)));
    recalcEstimatedSize(startTime, nextEnd, decoded);
    computeActualForSelection(startTime, nextEnd, decoded);
  }, [
    channels,
    computeActualForSelection,
    decoded,
    endTime,
    playhead,
    recalcEstimatedSize,
    startTime,
  ]);

  const onReset = useCallback(() => {
    if (!decoded) return;
    const nextStart = 0;
    let nextEnd = Math.min(decoded.duration, CHIME_MAX_DURATION_SECONDS);
    const maxDurationByBytes = maxFrames(channels) / CHIME_TARGET_SAMPLE_RATE;
    nextEnd = Math.min(nextEnd, nextStart + maxDurationByBytes);
    setNormalize(true);
    setPresetIndex(1);
    setStartTime(nextStart);
    setEndTime(nextEnd);
    setPlayhead(nextStart);
    stopPreview();
    recalcEstimatedSize(nextStart, nextEnd, decoded);
    computeActualForSelection(nextStart, nextEnd, decoded);
  }, [
    channels,
    computeActualForSelection,
    decoded,
    recalcEstimatedSize,
    stopPreview,
  ]);

  const onTogglePlay = useCallback(() => {
    if (!decoded || !engineRef.current || mode !== "ready") return;
    if (isPlaying) {
      stopPreview();
      return;
    }
    const processed = engineRef.current.extractProcessed({
      source: decoded,
      start: startTime,
      end: endTime,
      normalize,
      targetLufs: preset.lufs,
    });
    const durationSeconds = processed.frameCount / CHIME_TARGET_SAMPLE_RATE;
    setPlayhead(startTime);
    setIsPlaying(true);
    engineRef.current.preview(processed.pcm, (elapsed) => {
      const next = Math.min(endTime, startTime + elapsed);
      setPlayhead(next);
      if (elapsed >= durationSeconds) setIsPlaying(false);
    });
  }, [
    decoded,
    endTime,
    isPlaying,
    mode,
    normalize,
    preset.lufs,
    startTime,
    stopPreview,
  ]);

  const onUploadClick = useCallback(async () => {
    if (uploading) return;
    setError(null);
    setErrorRetryable(false);

    const normalizedBase = normalizeBaseFilename(filename);
    const filenameError = validateBaseFilename(normalizedBase);
    if (filenameError) {
      setError(filenameError);
      return;
    }

    if (mode === "fallback") {
      if (!rawWavUploadAllowed) {
        setError(NON_PROCESSABLE_AUDIO_MESSAGE);
        return;
      }
      setUploading(true);
      try {
        const bytes = new Uint8Array(await file.arrayBuffer());
        const valid = validateWav(bytes);
        if (!valid.ok) {
          setError(NON_PROCESSABLE_AUDIO_MESSAGE);
          return;
        }
        if (file.size > CHIME_MAX_BYTES) {
          setError(OVERSIZE_RAW_WAV_MESSAGE);
          return;
        }
        await onUpload(file);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Upload failed.");
        setErrorRetryable(isRetryableError(cause));
      } finally {
        setUploading(false);
      }
      return;
    }

    if (mode !== "ready" || !decoded || !engineRef.current) {
      setError(NON_PROCESSABLE_AUDIO_MESSAGE);
      return;
    }

    const processed = engineRef.current.extractProcessed({
      source: decoded,
      start: startTime,
      end: endTime,
      normalize,
      targetLufs: preset.lufs,
    });
    const encoded = encodeWavWithClipCount(processed.pcm);
    setActualSize(encoded.buffer.byteLength);
    setClipCount(encoded.clipCount);
    setSilentNormalizationSkipped(processed.silentNormalizationSkipped);

    if (encoded.buffer.byteLength > CHIME_MAX_BYTES) {
      setError("The generated WAV is over 1 MiB. Use Auto-Fit or shorten the trim.");
      return;
    }

    const output = new File([encoded.buffer], `${normalizedBase}.wav`, {
      type: "audio/wav",
    });
    if (output.size > CHIME_MAX_BYTES) {
      setError("The generated WAV is over 1 MiB. Use Auto-Fit or shorten the trim.");
      return;
    }

    setUploading(true);
    try {
      await onUpload(output);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Upload failed.");
      setErrorRetryable(isRetryableError(cause));
    } finally {
      setUploading(false);
    }
  }, [
    decoded,
    endTime,
    file,
    filename,
    mode,
    normalize,
    onUpload,
    preset.lufs,
    rawWavUploadAllowed,
    startTime,
    uploading,
  ]);

  const canUpload =
    !uploading &&
    (mode === "fallback"
      ? rawWavUploadAllowed && !overLimit
      : mode === "ready" && !overLimit);

  return (
    <section class="chime-editor-card" data-testid="chime-editor">
      {mode === "loading" && (
        <p class="chime-editor-loading" role="status">
          <span class="chime-spinner" aria-hidden="true" />
          Decoding audio…
        </p>
      )}

      {mode === "error" && (
        <p class="chime-upload-status fatal" role="alert">
          {error ?? NON_PROCESSABLE_AUDIO_MESSAGE}
        </p>
      )}

      {mode === "ready" && decoded && (
        <>
          <div class="chime-editor-waveform-wrap">
            <canvas
              ref={canvasRef}
              class="chime-editor-waveform"
              data-testid="chime-editor-waveform"
            />
          </div>

          <div class="chime-editor-controls">
            <label class="chime-editor-field">
              <span>Start Time</span>
              <input
                type="range"
                min={0}
                max={decoded.duration}
                step={0.01}
                value={startTime}
                data-testid="chime-trim-start"
                onInput={(event) =>
                  onStartChange(Number((event.currentTarget as HTMLInputElement).value))
                }
                onChange={(event) =>
                  computeActualForSelection(
                    Number((event.currentTarget as HTMLInputElement).value),
                    endTime,
                    decoded,
                  )
                }
              />
              <span class="chime-editor-readout">{toSecondsText(startTime)}</span>
            </label>

            <label class="chime-editor-field">
              <span>End Time</span>
              <input
                type="range"
                min={0}
                max={decoded.duration}
                step={0.01}
                value={endTime}
                data-testid="chime-trim-end"
                onInput={(event) =>
                  onEndChange(Number((event.currentTarget as HTMLInputElement).value))
                }
                onChange={(event) =>
                  computeActualForSelection(
                    startTime,
                    Number((event.currentTarget as HTMLInputElement).value),
                    decoded,
                  )
                }
              />
              <span class="chime-editor-readout">{toSecondsText(endTime)}</span>
            </label>
          </div>

          <div class="chime-editor-row">
            <label class="chime-editor-inline" for="chime-normalize-input">
              <input
                id="chime-normalize-input"
                type="checkbox"
                checked={normalize}
                data-testid="chime-normalize"
                onChange={(event) => {
                  setNormalize((event.currentTarget as HTMLInputElement).checked);
                }}
              />
              Normalize volume
            </label>

            <label class="chime-editor-field wide">
              <span>Approximate loudness preset</span>
              <input
                type="range"
                min={0}
                max={3}
                step={1}
                value={presetIndex}
                data-testid="chime-normalize-preset"
                onInput={(event) => {
                  setPresetIndex(
                    Number((event.currentTarget as HTMLInputElement).value),
                  );
                }}
              />
              <div class="chime-editor-preset-ticks" aria-hidden="true">
                {LOUDNESS_PRESETS.map((option, index) => (
                  <span
                    key={option.name}
                    class={
                      index === presetIndex
                        ? "chime-editor-preset-tick active"
                        : "chime-editor-preset-tick"
                    }
                  >
                    {option.name}
                  </span>
                ))}
              </div>
              <span class="chime-editor-readout">
                <strong>{preset.name}</strong> ({preset.lufs} LUFS)
              </span>
            </label>
          </div>

          <div class="chime-editor-preset-details">
            <strong>{preset.name}</strong>
            <p>{preset.description}</p>
            <p>Approximate loudness target: {preset.lufs} LUFS.</p>
          </div>

          <div class="chime-editor-stats" role="grid">
            <div role="gridcell">
              <span class="chime-editor-stats-label">Duration</span>
              <strong>{toSecondsText(decoded.duration)}</strong>
            </div>
            <div role="gridcell">
              <span class="chime-editor-stats-label">Effective Duration</span>
              <strong>{toSecondsText(Math.max(0, endTime - startTime))}</strong>
            </div>
            <div role="gridcell">
              <span class="chime-editor-stats-label">File Size</span>
              <strong data-testid="chime-stat-size">{toBytesText(actualSize)}</strong>
            </div>
            <div role="gridcell">
              <span class="chime-editor-stats-label">Status</span>
              <strong data-testid="chime-stat-status">{statusText}</strong>
            </div>
          </div>

          <div class="chime-editor-notes">
            {estimatedSize !== actualSize && (
              <p>Estimated while dragging: {toBytesText(estimatedSize)}</p>
            )}
            {clipCount > 0 && <p>Output may clip on some systems.</p>}
            {silentNormalizationSkipped && (
              <p>Silent / very quiet — normalization skipped.</p>
            )}
            {normalize && !silentNormalizationSkipped && !overLimit && (
              <p>Normalization gain is capped to +6 dB (2.0×).</p>
            )}
            {normalize && overLimit && (
              <p>The current trim exceeds 1 MiB after processing.</p>
            )}
          </div>

          <label class="chime-editor-field wide">
            <span>Output Filename</span>
            <div class="chime-editor-filename-row">
              <input
                type="text"
                value={filename}
                data-testid="chime-editor-filename"
                onInput={(event) =>
                  setFilename((event.currentTarget as HTMLInputElement).value)
                }
                aria-label="Output filename"
              />
              <span class="chime-editor-filename-suffix">.wav</span>
            </div>
          </label>

          <div class="chime-editor-actions">
            <button type="button" onClick={onTogglePlay}>
              {isPlaying ? "Stop" : "Play"}
            </button>
            <button
              type="button"
              data-testid="chime-editor-autofit"
              onClick={onAutoFit}
            >
              Auto-Fit to Limits
            </button>
            <button
              type="button"
              data-testid="chime-editor-reset"
              onClick={onReset}
            >
              Reset
            </button>
            <button
              type="button"
              data-testid="chime-editor-upload"
              onClick={() => void onUploadClick()}
              disabled={!canUpload}
              aria-busy={uploading ? "true" : "false"}
            >
              {uploading ? "Uploading…" : "Upload Trimmed Audio"}
            </button>
            <button
              type="button"
              data-testid="chime-editor-cancel"
              onClick={() => {
                stopPreview();
                onCancel();
              }}
            >
              Cancel
            </button>
          </div>
        </>
      )}

      {mode === "fallback" && (
        <>
          <p class="chime-upload-status retryable" role="status">
            Web Audio processing is unavailable. The original WAV can be uploaded
            unchanged.
          </p>
          <div class="chime-editor-actions">
            <button
              type="button"
              data-testid="chime-editor-upload"
              disabled={!canUpload}
              onClick={() => void onUploadClick()}
              aria-busy={uploading ? "true" : "false"}
            >
              {uploading ? "Uploading…" : "Upload Trimmed Audio"}
            </button>
            <button
              type="button"
              data-testid="chime-editor-cancel"
              onClick={onCancel}
            >
              Cancel
            </button>
          </div>
        </>
      )}

      {error && mode !== "error" && (
        <p
          class={`chime-upload-status ${errorRetryable ? "retryable" : "fatal"}`}
          role="alert"
        >
          {error}
        </p>
      )}
    </section>
  );
}
