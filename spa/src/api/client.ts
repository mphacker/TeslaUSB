/**
 * Typed client for the webd read-only catalog API (contract D2).
 *
 * webd is **read-only**: this client exposes GETs only — never mutations.
 * Errors surface as {@link ApiError} carrying the server's `{code, message}`
 * envelope when present. All paths are same-origin (`/api/...`) because the
 * bundle is served by webd itself; in dev, Vite proxies `/api` to webd.
 */
import type {
  Analytics,
  ApiErrorBody,
  Clip,
  DaySummary,
  EventItem,
  Page,
  Pref,
  Trip,
  TripDetail,
} from "./types";

/** An error from a webd API call (non-2xx, network, or malformed body). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function qs(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== undefined && v !== "",
  ) as [string, string | number][];
  if (entries.length === 0) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of entries) sp.set(k, String(v));
  return `?${sp.toString()}`;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      signal,
    });
  } catch (err) {
    throw new ApiError(0, "network", (err as Error).message || "network error");
  }
  const text = await resp.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      if (!resp.ok) {
        throw new ApiError(resp.status, "http_error", `HTTP ${resp.status}`);
      }
      throw new ApiError(resp.status, "bad_json", "malformed JSON response");
    }
  }
  if (!resp.ok) {
    const env = body as ApiErrorBody | null;
    const code = env?.error?.code ?? "http_error";
    const message = env?.error?.message ?? `HTTP ${resp.status}`;
    throw new ApiError(resp.status, code, message);
  }
  return body as T;
}

export interface EventsParams {
  after?: number;
  limit?: number;
  trip?: number;
}

export interface ClipsParams {
  after?: number;
  limit?: number;
  folder_class?: string;
}

export const api = {
  days: (signal?: AbortSignal) => getJson<DaySummary[]>("/api/days", signal),

  trips: (day?: string, signal?: AbortSignal) =>
    getJson<Trip[]>(`/api/trips${qs({ day })}`, signal),

  trip: (id: number, signal?: AbortSignal) =>
    getJson<TripDetail>(`/api/trips/${id}`, signal),

  events: (params: EventsParams = {}, signal?: AbortSignal) =>
    getJson<Page<EventItem>>(`/api/events${qs({ ...params })}`, signal),

  clips: (params: ClipsParams = {}, signal?: AbortSignal) =>
    getJson<Page<Clip>>(`/api/clips${qs({ ...params })}`, signal),

  clip: (id: number, signal?: AbortSignal) =>
    getJson<Clip>(`/api/clips/${id}`, signal),

  analytics: (signal?: AbortSignal) =>
    getJson<Analytics>("/api/analytics", signal),

  settings: (signal?: AbortSignal) => getJson<Pref[]>("/api/settings", signal),
};

export type Api = typeof api;
