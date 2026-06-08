/**
 * Tesla dashcam MP4 → telemetry parser.
 *
 * A faithful TypeScript port of the legacy `dashcam-mp4.js` box/frame walker,
 * paired with a tiny hand-written protobuf scalar decoder for the embedded
 * `SeiMetadata` message. We decode protobuf by hand (only the 16 scalar fields
 * the HUD needs) rather than pulling in `protobufjs`, so the SPA's dependency
 * set — and `package.json` — stays untouched.
 *
 * Production path only: real Tesla footage carries the SEI telemetry inline.
 * The UAT/seed path supplies telemetry via `window.__TESLAUSB_HUD_FIXTURE__`
 * instead (SMPTE test patterns have no SEI), so this parser failing closed
 * (returning `[]`) is expected and silent for those fixtures.
 *
 * Wire reference (field# / wire-type / meaning), from `dashcam.proto`:
 *   2  varint   gear_state            (enum 0=P,1=D,2=R,3=N)
 *   4  fixed32  vehicle_speed_mps     (float)
 *   5  fixed32  accelerator_pedal_pos (float)
 *   6  fixed32  steering_wheel_angle  (float, degrees)
 *   7  varint   blinker_on_left       (bool)
 *   8  varint   blinker_on_right      (bool)
 *   9  varint   brake_applied         (bool)
 *   10 varint   autopilot_state       (enum 0=NONE,1=SELF_DRIVING,2=AUTOSTEER,3=TACC)
 * (fields 1,3 and 11–16 — version, frame seq, GPS/accel doubles — are skipped.)
 */
import type { TelemetrySample } from "./telemetry";

interface DecodedSei {
  gear: number;
  speedMps: number;
  acceleratorPedalPosition: number;
  steeringAngle: number;
  blinkerLeft: boolean;
  blinkerRight: boolean;
  brakeApplied: boolean;
  autopilotState: number;
}

/** Decode the scalar `SeiMetadata` fields the HUD consumes from raw protobuf. */
function decodeSeiMetadata(bytes: Uint8Array): DecodedSei {
  const out: DecodedSei = {
    gear: 0,
    speedMps: 0,
    acceleratorPedalPosition: 0,
    steeringAngle: 0,
    blinkerLeft: false,
    blinkerRight: false,
    brakeApplied: false,
    autopilotState: 0,
  };
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let p = 0;

  const readVarint = (): number => {
    let shift = 0;
    let result = 0;
    while (p < bytes.length) {
      const b = bytes[p++];
      result |= (b & 0x7f) << shift;
      if ((b & 0x80) === 0) break;
      shift += 7;
    }
    return result >>> 0;
  };

  while (p < bytes.length) {
    const tag = readVarint();
    const field = tag >>> 3;
    const wire = tag & 0x07;
    switch (wire) {
      case 0: {
        // varint
        const v = readVarint();
        if (field === 2) out.gear = v;
        else if (field === 7) out.blinkerLeft = v !== 0;
        else if (field === 8) out.blinkerRight = v !== 0;
        else if (field === 9) out.brakeApplied = v !== 0;
        else if (field === 10) out.autopilotState = v;
        break;
      }
      case 5: {
        // 32-bit (float)
        if (p + 4 > bytes.length) return out;
        const f = view.getFloat32(p, true);
        p += 4;
        if (field === 4) out.speedMps = f;
        else if (field === 5) out.acceleratorPedalPosition = f;
        else if (field === 6) out.steeringAngle = f;
        break;
      }
      case 1: {
        // 64-bit (double) — skipped fields (GPS/accel)
        p += 8;
        break;
      }
      case 2: {
        // length-delimited — not expected; skip safely
        const len = readVarint();
        p += len;
        break;
      }
      default:
        return out; // unknown wire type → stop, return what we have
    }
  }
  return out;
}

/** MP4 box walker + frame/SEI extractor (port of legacy `DashcamMP4`). */
class DashcamMp4 {
  private view: DataView;
  private buffer: ArrayBuffer;

  constructor(buffer: ArrayBuffer) {
    this.buffer = buffer;
    this.view = new DataView(buffer);
  }

  private readAscii(start: number, len: number): string {
    let s = "";
    for (let i = 0; i < len; i++) s += String.fromCharCode(this.view.getUint8(start + i));
    return s;
  }

  private findBox(
    start: number,
    end: number,
    name: string,
  ): { start: number; end: number; size: number } {
    for (let pos = start; pos + 8 <= end; ) {
      let size = this.view.getUint32(pos);
      const type = this.readAscii(pos + 4, 4);
      const headerSize = size === 1 ? 16 : 8;
      if (size === 1) {
        const high = this.view.getUint32(pos + 8);
        const low = this.view.getUint32(pos + 12);
        size = Number((BigInt(high) << 32n) | BigInt(low));
      } else if (size === 0) {
        size = end - pos;
      }
      if (type === name) {
        return { start: pos + headerSize, end: pos + size, size: size - headerSize };
      }
      if (size <= 0) break;
      pos += size;
    }
    throw new Error(`Box "${name}" not found`);
  }

  /** Per-frame durations in milliseconds, derived from stts + mdhd timescale. */
  private frameDurations(): number[] {
    const moov = this.findBox(0, this.view.byteLength, "moov");
    const trak = this.findBox(moov.start, moov.end, "trak");
    const mdia = this.findBox(trak.start, trak.end, "mdia");
    const minf = this.findBox(mdia.start, mdia.end, "minf");
    const stbl = this.findBox(minf.start, minf.end, "stbl");

    const mdhd = this.findBox(mdia.start, mdia.end, "mdhd");
    const mdhdVersion = this.view.getUint8(mdhd.start);
    const timescale =
      mdhdVersion === 1
        ? this.view.getUint32(mdhd.start + 20)
        : this.view.getUint32(mdhd.start + 12);

    const stts = this.findBox(stbl.start, stbl.end, "stts");
    const entryCount = this.view.getUint32(stts.start + 4);
    const durations: number[] = [];
    let pos = stts.start + 8;
    for (let i = 0; i < entryCount; i++) {
      const count = this.view.getUint32(pos);
      const delta = this.view.getUint32(pos + 4);
      const ms = (delta / timescale) * 1000;
      for (let j = 0; j < count; j++) durations.push(ms);
      pos += 8;
    }
    return durations;
  }

  /** Strip H.264 emulation-prevention bytes (0x000003 → 0x0000). */
  private stripEmulationBytes(data: Uint8Array): Uint8Array {
    const out: number[] = [];
    let zeros = 0;
    for (const byte of data) {
      if (zeros >= 2 && byte === 0x03) {
        zeros = 0;
        continue;
      }
      out.push(byte);
      zeros = byte === 0 ? zeros + 1 : 0;
    }
    return Uint8Array.from(out);
  }

  /** Decode a SEI NAL unit into telemetry, or `null` if it isn't Tesla SEI. */
  private decodeSei(nal: Uint8Array): DecodedSei | null {
    if (nal.length < 4) return null;
    let i = 3;
    while (i < nal.length && nal[i] === 0x42) i++;
    if (i <= 3 || i + 1 >= nal.length || nal[i] !== 0x69) return null;
    try {
      return decodeSeiMetadata(this.stripEmulationBytes(nal.subarray(i + 1, nal.length - 1)));
    } catch {
      return null;
    }
  }

  /** Walk mdat NAL units, attaching each SEI to the following frame's start time. */
  parseSamples(): TelemetrySample[] {
    const durations = this.frameDurations();
    const mdat = this.findBox(0, this.view.byteLength, "mdat");
    const samples: TelemetrySample[] = [];
    let cursor = mdat.start;
    const end = mdat.start + mdat.size;
    let pendingSei: DecodedSei | null = null;
    let frameIndex = 0;
    let tMs = 0;

    while (cursor + 4 <= end) {
      const len = this.view.getUint32(cursor);
      cursor += 4;
      if (len < 1 || cursor + len > this.view.byteLength) break;
      const type = this.view.getUint8(cursor) & 0x1f;
      if (type === 6) {
        pendingSei = this.decodeSei(new Uint8Array(this.buffer.slice(cursor, cursor + len)));
      } else if (type === 5 || type === 1) {
        const time = tMs / 1000;
        if (pendingSei) {
          samples.push({ time, ...pendingSei });
        }
        pendingSei = null;
        tMs += durations[frameIndex] ?? durations[durations.length - 1] ?? 1000 / 30;
        frameIndex++;
      }
      cursor += len;
    }
    return samples;
  }
}

/**
 * Parse the embedded Tesla telemetry track out of a dashcam MP4. Returns a
 * time-sorted sample list, or `[]` for any MP4 that lacks Tesla SEI (e.g. the
 * SMPTE UAT fixtures) or that fails to parse — failing closed, never throwing.
 */
export function parseSeiTelemetry(buffer: ArrayBuffer): TelemetrySample[] {
  try {
    return new DashcamMp4(buffer).parseSamples();
  } catch {
    return [];
  }
}
