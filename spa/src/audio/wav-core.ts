export const CHIME_MAX_BYTES = 1_048_576;
export const CHIME_TARGET_SAMPLE_RATE = 44_100;
export const CHIME_MAX_GAIN = 2.0;
export const CHIME_RMS_EPSILON = 1e-6;

export interface PcmAudio {
  channels: Float32Array[];
  sampleRate: number;
}

export interface WavValidationOk {
  ok: true;
}

export interface WavValidationFail {
  ok: false;
  reason: string;
}

export type WavValidation = WavValidationOk | WavValidationFail;

export interface QuantizeResult {
  samples: Int16Array;
  clipCount: number;
}

export interface EncodedWavWithClipCount {
  buffer: ArrayBuffer;
  clipCount: number;
}

function fail(reason: string): WavValidationFail {
  return { ok: false, reason };
}

function readTag(bytes: Uint8Array, offset: number): string {
  return String.fromCharCode(
    bytes[offset],
    bytes[offset + 1],
    bytes[offset + 2],
    bytes[offset + 3],
  );
}

function asUint8Array(bytes: ArrayBuffer | Uint8Array | DataView): Uint8Array {
  if (bytes instanceof Uint8Array) return bytes;
  if (bytes instanceof DataView) {
    return new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  }
  return new Uint8Array(bytes);
}

function frameCountForChannels(channels: Float32Array[]): number {
  if (channels.length === 0) return 0;
  return channels.reduce(
    (minLength, channel) => Math.min(minLength, channel.length),
    Number.POSITIVE_INFINITY,
  );
}

export function maxFrames(
  channels: number,
  maxBytes = CHIME_MAX_BYTES,
): number {
  if (channels <= 0 || maxBytes <= 44) return 0;
  return Math.floor((maxBytes - 44) / (channels * 2));
}

export function downmixToMono(channels: Float32Array[]): Float32Array[] {
  if (channels.length === 0) return [];
  if (channels.length === 1) return [channels[0].slice()];
  const frames = frameCountForChannels(channels);
  const mono = new Float32Array(frames);
  for (let i = 0; i < frames; i++) {
    let sum = 0;
    for (const channel of channels) sum += channel[i];
    mono[i] = sum / channels.length;
  }
  return [mono];
}

export function rms(pcm: PcmAudio): number {
  const frames = frameCountForChannels(pcm.channels);
  if (frames === 0) return 0;
  let sumSquares = 0;
  let sampleCount = 0;
  for (const channel of pcm.channels) {
    for (let i = 0; i < frames; i++) {
      const sample = channel[i];
      sumSquares += sample * sample;
      sampleCount += 1;
    }
  }
  return sampleCount === 0 ? 0 : Math.sqrt(sumSquares / sampleCount);
}

export function approxLufs(rmsValue: number): number {
  if (!Number.isFinite(rmsValue) || rmsValue < CHIME_RMS_EPSILON) {
    return Number.NEGATIVE_INFINITY;
  }
  return 20 * Math.log10(rmsValue) - 0.691;
}

export function normalizationGain(
  currentLufs: number,
  targetLufs: number,
): number {
  if (!Number.isFinite(currentLufs)) return 1.0;
  const gain = 10 ** ((targetLufs - currentLufs) / 20);
  if (!Number.isFinite(gain) || gain <= 0) return 1.0;
  return Math.min(gain, CHIME_MAX_GAIN);
}

export function quantizeWithClipCount(
  pcm: PcmAudio,
  frameLimit?: number,
): QuantizeResult {
  const channelCount = pcm.channels.length;
  if (channelCount === 0) {
    return { samples: new Int16Array(0), clipCount: 0 };
  }
  const sourceFrames = frameCountForChannels(pcm.channels);
  const frames =
    frameLimit == null
      ? sourceFrames
      : Math.max(0, Math.min(sourceFrames, frameLimit));
  const out = new Int16Array(frames * channelCount);
  let clipCount = 0;
  let outIndex = 0;
  for (let frame = 0; frame < frames; frame++) {
    for (let ch = 0; ch < channelCount; ch++) {
      let sample = pcm.channels[ch][frame];
      if (!Number.isFinite(sample)) sample = 0;
      const clamped = Math.max(-1, Math.min(1, sample));
      if (sample !== clamped) clipCount += 1;
      const q = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      const int16 = Math.max(-32768, Math.min(32767, Math.round(q)));
      out[outIndex++] = int16;
    }
  }
  return { samples: out, clipCount };
}

export function encodeWavWithClipCount(
  pcm: PcmAudio,
  frameLimit?: number,
): EncodedWavWithClipCount {
  const channelCount = pcm.channels.length;
  if (channelCount === 0) {
    return { buffer: new ArrayBuffer(44), clipCount: 0 };
  }
  const { samples, clipCount } = quantizeWithClipCount(pcm, frameLimit);
  const dataSize = samples.byteLength;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);
  const bytes = new Uint8Array(buffer);

  bytes.set([0x52, 0x49, 0x46, 0x46], 0); // RIFF
  view.setUint32(4, 36 + dataSize, true);
  bytes.set([0x57, 0x41, 0x56, 0x45], 8); // WAVE
  bytes.set([0x66, 0x6d, 0x74, 0x20], 12); // fmt
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, channelCount, true);
  view.setUint32(24, pcm.sampleRate, true);
  const blockAlign = channelCount * 2;
  view.setUint32(28, pcm.sampleRate * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, 16, true);
  bytes.set([0x64, 0x61, 0x74, 0x61], 36); // data
  view.setUint32(40, dataSize, true);

  new Int16Array(buffer, 44, samples.length).set(samples);
  return { buffer, clipCount };
}

export function encodeWav(pcm: PcmAudio): ArrayBuffer {
  return encodeWavWithClipCount(pcm).buffer;
}

export function validateWav(
  bytesLike: ArrayBuffer | Uint8Array | DataView,
): WavValidation {
  const bytes = asUint8Array(bytesLike);
  if (bytes.byteLength < 12) return fail("file too small to be a WAV");
  if (readTag(bytes, 0) !== "RIFF") return fail("missing RIFF header");
  if (readTag(bytes, 8) !== "WAVE") return fail("missing WAVE form type");

  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 12;
  let fmtSeen = false;
  let dataNonEmpty = false;

  while (offset + 8 <= bytes.byteLength) {
    const chunkSize = view.getUint32(offset + 4, true);
    const bodyStart = offset + 8;
    const bodyEnd = bodyStart + chunkSize;
    if (bodyEnd > bytes.byteLength) return fail("chunk body exceeds file");

    const chunkId = readTag(bytes, offset);
    if (chunkId === "fmt ") {
      if (chunkSize < 16) return fail("fmt chunk too small");
      const audioFormat = view.getUint16(bodyStart, true);
      const channels = view.getUint16(bodyStart + 2, true);
      const sampleRate = view.getUint32(bodyStart + 4, true);
      const byteRate = view.getUint32(bodyStart + 8, true);
      const blockAlign = view.getUint16(bodyStart + 12, true);
      const bits = view.getUint16(bodyStart + 14, true);

      if (audioFormat !== 1) {
        return fail("only PCM (format 1) lock chimes are supported");
      }
      if (channels !== 1 && channels !== 2) {
        return fail("lock chime must be mono or stereo");
      }
      if (sampleRate !== 44_100 && sampleRate !== 48_000) {
        return fail("lock chime sample rate must be 44.1 or 48 kHz");
      }
      if (bits !== 16) {
        return fail("lock chime must be 16-bit PCM");
      }
      const expectedBlock = channels * (bits / 8);
      if (blockAlign !== expectedBlock) {
        return fail("fmt block_align inconsistent with channels/bits");
      }
      const expectedByteRate = sampleRate * expectedBlock;
      if (byteRate !== expectedByteRate) {
        return fail("fmt byte_rate inconsistent with rate/channels/bits");
      }
      fmtSeen = true;
    } else if (chunkId === "data" && chunkSize > 0) {
      dataNonEmpty = true;
    }

    const consumed = 8 + chunkSize + (chunkSize % 2);
    const next = offset + consumed;
    if (next < offset || next > bytes.byteLength + 1) {
      return fail("offset overflow");
    }
    offset = next;
  }

  if (!fmtSeen) return fail("missing fmt chunk");
  if (!dataNonEmpty) return fail("missing or empty data chunk");
  return { ok: true };
}
