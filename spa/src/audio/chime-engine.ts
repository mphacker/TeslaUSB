import {
  CHIME_MAX_BYTES,
  CHIME_RMS_EPSILON,
  CHIME_TARGET_SAMPLE_RATE,
  approxLufs,
  downmixToMono,
  maxFrames,
  normalizationGain,
  quantizeWithClipCount,
  rms,
  type PcmAudio,
} from "./wav-core";

export interface DecodedAudio extends PcmAudio {
  duration: number;
}

export interface ExtractOptions {
  source: PcmAudio;
  start: number;
  end: number;
  normalize: boolean;
  targetLufs: number;
  maxBytes?: number;
}

export interface ProcessedAudio {
  pcm: PcmAudio;
  frameCount: number;
  estimatedBytes: number;
  clipCount: number;
  gain: number;
  silentNormalizationSkipped: boolean;
}

export class WebAudioUnavailableError extends Error {
  constructor(
    message = "This browser can't process this audio — please upload a 16-bit PCM WAV.",
  ) {
    super(message);
    this.name = "WebAudioUnavailableError";
  }
}

export class ChimeAudioEngine {
  private context: AudioContext | null = null;
  private currentSource: AudioBufferSourceNode | null = null;
  private rafId: number | null = null;
  private token = 0;

  private nextToken(): number {
    this.token += 1;
    return this.token;
  }

  private isCurrentToken(token: number): boolean {
    return token === this.token;
  }

  private getAudioContext(): AudioContext {
    if (typeof window === "undefined") {
      throw new WebAudioUnavailableError();
    }
    const Ctor =
      window.AudioContext ??
      (window as typeof window & { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!Ctor) throw new WebAudioUnavailableError();
    if (!this.context) this.context = new Ctor();
    return this.context;
  }

  private createOfflineContext(
    channels: number,
    length: number,
    sampleRate: number,
  ): OfflineAudioContext {
    const Ctor =
      window.OfflineAudioContext ??
      (window as typeof window & {
        webkitOfflineAudioContext?: typeof OfflineAudioContext;
      }).webkitOfflineAudioContext;
    if (!Ctor) throw new WebAudioUnavailableError();
    return new Ctor(channels, Math.max(1, length), sampleRate);
  }

  async decode(file: Blob): Promise<DecodedAudio> {
    const token = this.nextToken();
    const context = this.getAudioContext();
    const bytes = await file.arrayBuffer();
    if (!this.isCurrentToken(token)) {
      throw new Error("stale decode result");
    }

    const decoded = await context.decodeAudioData(bytes.slice(0));
    if (!this.isCurrentToken(token)) {
      throw new Error("stale decode result");
    }

    if (decoded.sampleRate === CHIME_TARGET_SAMPLE_RATE) {
      return {
        channels: Array.from(
          { length: decoded.numberOfChannels },
          (_, i) => decoded.getChannelData(i).slice(),
        ),
        sampleRate: decoded.sampleRate,
        duration: decoded.duration,
      };
    }

    const targetLength = Math.ceil(
      decoded.length * (CHIME_TARGET_SAMPLE_RATE / decoded.sampleRate),
    );
    const offline = this.createOfflineContext(
      decoded.numberOfChannels,
      targetLength,
      CHIME_TARGET_SAMPLE_RATE,
    );
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start(0);
    const rendered = await offline.startRendering();
    if (!this.isCurrentToken(token)) {
      throw new Error("stale decode result");
    }
    return {
      channels: Array.from(
        { length: rendered.numberOfChannels },
        (_, i) => rendered.getChannelData(i).slice(),
      ),
      sampleRate: rendered.sampleRate,
      duration: rendered.duration,
    };
  }

  extractProcessed({
    source,
    start,
    end,
    normalize,
    targetLufs,
    maxBytes = CHIME_MAX_BYTES,
  }: ExtractOptions): ProcessedAudio {
    const frameCount = source.channels.length
      ? Math.min(...source.channels.map((channel) => channel.length))
      : 0;
    const startFrame = Math.max(
      0,
      Math.min(frameCount, Math.floor(start * source.sampleRate)),
    );
    const endFrame = Math.max(
      startFrame,
      Math.min(frameCount, Math.floor(end * source.sampleRate)),
    );

    const trimmed = source.channels.map((channel) =>
      channel.slice(startFrame, endFrame),
    );
    const processedChannels =
      trimmed.length > 2 ? downmixToMono(trimmed) : trimmed;
    const outputChannelCount = Math.max(1, processedChannels.length);
    const outputFrames =
      processedChannels.length === 0 ? 0 : processedChannels[0].length;

    const loudnessRms =
      processedChannels.length === 0
        ? 0
        : rms({
            channels: processedChannels,
            sampleRate: CHIME_TARGET_SAMPLE_RATE,
          });
    const isSilent = loudnessRms < CHIME_RMS_EPSILON;
    const gain =
      normalize && !isSilent
        ? normalizationGain(approxLufs(loudnessRms), targetLufs)
        : 1.0;

    const gained = processedChannels.map((channel) => {
      if (gain === 1.0) return channel.slice();
      const next = new Float32Array(channel.length);
      for (let i = 0; i < channel.length; i++) {
        next[i] = channel[i] * gain;
      }
      return next;
    });

    const allowedFrames = maxFrames(outputChannelCount, maxBytes);
    const clampedFrames = Math.min(outputFrames, allowedFrames);
    const clampedChannels = gained.map((channel) =>
      channel.slice(0, clampedFrames),
    );
    const clipCount = quantizeWithClipCount(
      { channels: clampedChannels, sampleRate: CHIME_TARGET_SAMPLE_RATE },
      clampedFrames,
    ).clipCount;

    return {
      pcm: {
        channels: clampedChannels,
        sampleRate: CHIME_TARGET_SAMPLE_RATE,
      },
      frameCount: clampedFrames,
      estimatedBytes: 44 + clampedFrames * outputChannelCount * 2,
      clipCount,
      gain,
      silentNormalizationSkipped: normalize && isSilent,
    };
  }

  waveformPeaks(channels: Float32Array[], points: number): Float32Array {
    const out = new Float32Array(points);
    if (points <= 0 || channels.length === 0) return out;
    const frames = Math.min(...channels.map((channel) => channel.length));
    if (frames <= 0) return out;

    for (let bucket = 0; bucket < points; bucket++) {
      const start = Math.floor((bucket * frames) / points);
      const end = Math.floor(((bucket + 1) * frames) / points);
      let peak = 0;
      for (let i = start; i < end; i++) {
        for (const channel of channels) {
          const value = Math.abs(channel[i]);
          if (value > peak) peak = value;
        }
      }
      out[bucket] = peak;
    }
    return out;
  }

  preview(
    processedPcm: PcmAudio,
    onPlayhead?: (seconds: number) => void,
  ): void {
    this.stop();
    const token = this.nextToken();
    const context = this.getAudioContext();
    const frames = processedPcm.channels.length
      ? Math.min(...processedPcm.channels.map((channel) => channel.length))
      : 0;
    const buffer = context.createBuffer(
      Math.max(1, processedPcm.channels.length),
      Math.max(1, frames),
      processedPcm.sampleRate,
    );
    for (let ch = 0; ch < processedPcm.channels.length; ch++) {
      buffer.copyToChannel(new Float32Array(processedPcm.channels[ch]), ch);
    }

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.playbackRate.value = 1.0;
    source.connect(context.destination);
    const startAt = context.currentTime;
    source.onended = () => {
      if (!this.isCurrentToken(token)) return;
      if (this.rafId != null) window.cancelAnimationFrame(this.rafId);
      this.rafId = null;
      this.currentSource = null;
      onPlayhead?.(buffer.duration);
    };
    source.start(0);
    this.currentSource = source;

    const tick = () => {
      if (!this.isCurrentToken(token)) return;
      const elapsed = Math.max(0, context.currentTime - startAt);
      onPlayhead?.(Math.min(elapsed, buffer.duration));
      if (elapsed < buffer.duration) {
        this.rafId = window.requestAnimationFrame(tick);
      }
    };
    this.rafId = window.requestAnimationFrame(tick);
  }

  stop(): void {
    this.nextToken();
    if (this.rafId != null) {
      window.cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    if (this.currentSource) {
      try {
        this.currentSource.stop();
      } catch {
        // already stopped
      }
      this.currentSource.disconnect();
      this.currentSource = null;
    }
  }

  dispose(): void {
    this.stop();
    if (this.context) {
      void this.context.close();
      this.context = null;
    }
  }
}
