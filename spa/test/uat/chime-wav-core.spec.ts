import { expect, test } from "@playwright/test";
import {
  CHIME_MAX_BYTES,
  approxLufs,
  downmixToMono,
  encodeWav,
  encodeWavWithClipCount,
  maxFrames,
  normalizationGain,
  quantizeWithClipCount,
  rms,
  validateWav,
  type PcmAudio,
} from "../../src/audio/wav-core";

function makeWav({
  channels = 1,
  sampleRate = 44_100,
  bits = 16,
  dataLen = 8,
}: {
  channels?: number;
  sampleRate?: number;
  bits?: number;
  dataLen?: number;
}) {
  const blockAlign = channels * (bits / 8);
  const byteRate = sampleRate * blockAlign;
  const bytes = new Uint8Array(44 + dataLen);
  const view = new DataView(bytes.buffer);
  bytes.set([0x52, 0x49, 0x46, 0x46], 0); // RIFF
  view.setUint32(4, 36 + dataLen, true);
  bytes.set([0x57, 0x41, 0x56, 0x45], 8); // WAVE
  bytes.set([0x66, 0x6d, 0x74, 0x20], 12); // fmt
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bits, true);
  bytes.set([0x64, 0x61, 0x74, 0x61], 36); // data
  view.setUint32(40, dataLen, true);
  return bytes;
}

test("validateWav accepts mono 44.1 kHz and stereo 48 kHz PCM16", () => {
  expect(validateWav(makeWav({ channels: 1, sampleRate: 44_100 })).ok).toBe(true);
  expect(validateWav(makeWav({ channels: 2, sampleRate: 48_000 })).ok).toBe(true);
});

test("validateWav rejects unsupported bit depth, channels, sample rate, empty data, and truncated bytes", () => {
  expect(validateWav(makeWav({ bits: 8 }))).toEqual({
    ok: false,
    reason: "lock chime must be 16-bit PCM",
  });
  expect(validateWav(makeWav({ channels: 3 }))).toEqual({
    ok: false,
    reason: "lock chime must be mono or stereo",
  });
  expect(validateWav(makeWav({ sampleRate: 22_050 }))).toEqual({
    ok: false,
    reason: "lock chime sample rate must be 44.1 or 48 kHz",
  });
  expect(validateWav(makeWav({ dataLen: 0 }))).toEqual({
    ok: false,
    reason: "missing or empty data chunk",
  });
  expect(validateWav(new Uint8Array([0x52, 0x49, 0x46, 0x46]))).toEqual({
    ok: false,
    reason: "file too small to be a WAV",
  });
});

test("encodeWav writes canonical RIFF/WAVE headers with correct byteRate and blockAlign", () => {
  const pcm: PcmAudio = {
    sampleRate: 44_100,
    channels: [new Float32Array([0.25, -0.25]), new Float32Array([0.5, -0.5])],
  };
  const wav = encodeWav(pcm);
  const view = new DataView(wav);
  const bytes = new Uint8Array(wav);
  expect(String.fromCharCode(...bytes.subarray(0, 4))).toBe("RIFF");
  expect(String.fromCharCode(...bytes.subarray(8, 12))).toBe("WAVE");
  expect(view.getUint16(20, true)).toBe(1);
  expect(view.getUint16(22, true)).toBe(2);
  expect(view.getUint32(24, true)).toBe(44_100);
  expect(view.getUint32(28, true)).toBe(44_100 * 4);
  expect(view.getUint16(32, true)).toBe(4);
  expect(view.getUint16(34, true)).toBe(16);
});

test("maxFrames keeps stereo export under 1 MiB", () => {
  const frames = maxFrames(2, CHIME_MAX_BYTES);
  const pcm: PcmAudio = {
    sampleRate: 44_100,
    channels: [new Float32Array(frames + 1), new Float32Array(frames + 1)],
  };
  const encoded = encodeWavWithClipCount(pcm, frames);
  expect(encoded.buffer.byteLength).toBeLessThanOrEqual(CHIME_MAX_BYTES);
});

test("downmixToMono averages >2 channels into one channel", () => {
  const mixed = downmixToMono([
    new Float32Array([0.3, 0.1]),
    new Float32Array([0.1, 0.1]),
    new Float32Array([0.2, -0.2]),
  ]);
  expect(mixed).toHaveLength(1);
  expect(mixed[0][0]).toBeCloseTo(0.2, 6);
  expect(mixed[0][1]).toBeCloseTo(0, 6);
});

test("rms and approxLufs handle silence and normalizationGain handles silence and +6 dB clamp", () => {
  const silent: PcmAudio = {
    sampleRate: 44_100,
    channels: [new Float32Array([0, 0, 0])],
  };
  expect(rms(silent)).toBe(0);
  expect(approxLufs(rms(silent))).toBe(Number.NEGATIVE_INFINITY);
  expect(normalizationGain(Number.NEGATIVE_INFINITY, -16)).toBe(1);
  expect(normalizationGain(-40, -12)).toBe(2);
});

test("quantizeWithClipCount counts clipped samples", () => {
  const clipped: PcmAudio = {
    sampleRate: 44_100,
    channels: [new Float32Array([1.2, -1.3, 0.5])],
  };
  const quantized = quantizeWithClipCount(clipped);
  expect(quantized.clipCount).toBeGreaterThan(0);
  expect(quantized.samples.length).toBe(3);
});
