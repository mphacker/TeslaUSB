import type { Angle } from "../api/types";

export const VIEW_ARCHIVE = "archive";

export function isStreamableAngle(angle: Angle | undefined): boolean {
  if (!angle) return false;
  return angle.view_kind.trim().toLowerCase() !== "unavailable";
}

export function isDownloadableAngle(angle: Angle | undefined): boolean {
  return angle?.view_kind === VIEW_ARCHIVE;
}
