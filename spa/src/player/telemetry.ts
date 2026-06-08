/**
 * Telemetry model shared by the event-player HUD: the per-sample vehicle state
 * extracted from Tesla dashcam footage, and the derived, render-ready HUD state.
 *
 * The legacy player drove its HUD from the `SeiMetadata` protobuf embedded in
 * each dashcam MP4 (decoded by {@link ./dashcam-mp4}). The fields and the
 * value→HUD mappings below are a faithful port of `event_player.html`'s
 * `updateHUD()` (speed × 2.23694 → mph, gear letter, steering degrees as the
 * wheel rotation, binary brake, pedal-position throttle, autopilot label). No
 * new telemetry fields are invented here — these are exactly the legacy SEI
 * fields.
 */

/** One decoded telemetry sample, time-aligned to the clip's playback clock. */
export interface TelemetrySample {
  /** Seconds into the clip (video `currentTime` space). */
  time: number;
  /** `vehicle_speed_mps` — metres/second (may be negative in reverse). */
  speedMps: number;
  /** `gear_state` enum ordinal (0=P, 1=D, 2=R, 3=N). */
  gear: number;
  /** `steering_wheel_angle` — degrees (used directly as wheel rotation). */
  steeringAngle: number;
  /** `blinker_on_left`. */
  blinkerLeft: boolean;
  /** `blinker_on_right`. */
  blinkerRight: boolean;
  /** `brake_applied` (binary brake pedal in the legacy HUD). */
  brakeApplied: boolean;
  /** `accelerator_pedal_position` — 0..1 or 0..100 (normalised on render). */
  acceleratorPedalPosition: number;
  /** `autopilot_state` enum ordinal (0=NONE, 1=SELF_DRIVING, 2=AUTOSTEER, 3=TACC). */
  autopilotState: number;
}

/** Render-ready HUD state — one value per HUD widget. */
export interface HudState {
  /** Speed, rounded mph (always non-negative, matching the legacy display). */
  speed: number;
  /** Gear letter (P/D/R/N). */
  gear: string;
  /** Steering-wheel rotation, degrees. */
  steering: number;
  /** Brake pedal fill, 0–100 (%). */
  brakeFill: number;
  /** Throttle pedal fill, 0–100 (%). */
  throttleFill: number;
  /** Left blinker active. */
  blinkerLeft: boolean;
  /** Right blinker active. */
  blinkerRight: boolean;
  /** Autopilot label (empty when not engaged). */
  autopilot: string;
  /** Whether the autopilot indicator is highlighted. */
  autopilotActive: boolean;
}

const MPS_TO_MPH = 2.23694;

const GEAR_LETTERS = ["P", "D", "R", "N"];

/** Friendly autopilot labels (enum ordinal → display text), matching the
 *  legacy server-rendered casing (e.g. "Self-Driving"). */
const AUTOPILOT_LABELS: Record<number, string> = {
  1: "Self-Driving",
  2: "Autosteer",
  3: "TACC",
};

/** The neutral HUD state shown when no telemetry covers the current time. */
export const DEFAULT_HUD: HudState = {
  speed: 0,
  gear: "P",
  steering: 0,
  brakeFill: 0,
  throttleFill: 0,
  blinkerLeft: false,
  blinkerRight: false,
  autopilot: "",
  autopilotActive: false,
};

/** Map a telemetry sample onto render-ready HUD state (port of `updateHUD`). */
export function sampleToHud(sample: TelemetrySample): HudState {
  // Throttle: the pedal position may be 0..1 or 0..100; scale the former.
  let throttle = sample.acceleratorPedalPosition || 0;
  if (throttle <= 1.2) throttle *= 100;
  throttle = Math.min(100, Math.max(0, throttle));

  const apActive = sample.autopilotState !== 0;

  return {
    speed: Math.round(Math.abs(sample.speedMps || 0) * MPS_TO_MPH),
    gear: GEAR_LETTERS[sample.gear] ?? "P",
    steering: sample.steeringAngle || 0,
    brakeFill: sample.brakeApplied ? 100 : 0,
    throttleFill: throttle,
    blinkerLeft: sample.blinkerLeft === true,
    blinkerRight: sample.blinkerRight === true,
    autopilot: apActive ? (AUTOPILOT_LABELS[sample.autopilotState] ?? "") : "",
    autopilotActive: apActive,
  };
}

/**
 * Find the telemetry sample in effect at `time` (the last sample whose `time`
 * is ≤ the playback clock). Samples are assumed sorted ascending by `time`.
 * Returns `null` before the first sample so the HUD shows its neutral default.
 */
export function sampleAt(
  samples: TelemetrySample[],
  time: number,
): TelemetrySample | null {
  let found: TelemetrySample | null = null;
  for (const s of samples) {
    if (s.time <= time) found = s;
    else break;
  }
  return found;
}

/**
 * The optional UAT/seed telemetry source. The Playwright UAT injects a sample
 * track here (the SMPTE test-pattern fixtures carry no embedded SEI), exactly
 * as the 0.4 parity baseline did ("generated SMPTE test-pattern MP4s; HUD uses
 * seeded values"). In production this global is absent and the controller
 * parses the real SEI telemetry out of the streamed MP4 instead.
 */
export function fixtureTelemetry(): TelemetrySample[] | null {
  const w = window as unknown as {
    __TESLAUSB_HUD_FIXTURE__?: TelemetrySample[];
  };
  const f = w.__TESLAUSB_HUD_FIXTURE__;
  return Array.isArray(f) && f.length > 0 ? f : null;
}
