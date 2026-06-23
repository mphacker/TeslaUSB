import { test, expect } from "@playwright/test";
import { loadState } from "./helpers";
import { api, ApiError } from "../../src/api/client";

// ── Task 5.2 §4: client-level tests for the webd read-only catalog client ──
// Exercises the ACTUAL typed client (src/api/client.ts) against the live seeded
// webd from global-setup. The client issues same-origin relative GETs ("/api/…")
// because in production the bundle is served by webd; here we shim global fetch
// to resolve those relative paths against the UAT server's origin. This proves
// the client's URL building, JSON decoding, pagination shapes and ApiError
// envelope handling — readying the remaining 5.3 screens, which reuse it.
//
// These reads are idempotent, so running once per viewport project is harmless.

const realFetch = globalThis.fetch;

test.beforeAll(() => {
  const base = new URL(loadState().baseURL).origin;
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (typeof input === "string" && input.startsWith("/")) {
      return realFetch(base + input, init);
    }
    return realFetch(input as RequestInfo, init);
  }) as typeof fetch;
});

test.afterAll(() => {
  globalThis.fetch = realFetch;
});

test.describe("webd catalog client", () => {
  test("days() returns the seeded civil day with rollups", async () => {
    const days = await api.days();
    expect(Array.isArray(days)).toBe(true);
    expect(days.length).toBe(1);
    const d = days[0];
    expect(d.day).toBe("2024-06-01");
    expect(d.trip_count).toBe(3);
    // webd counts only trip-linked events (the trip-less sentry is excluded).
    expect(d.event_count).toBe(2);
    expect(d.distance_m).toBeGreaterThan(0);
  });

  test("trips() lists all trips; day filter narrows", async () => {
    const all = await api.trips();
    expect(all.length).toBe(3);
    for (const t of all) {
      expect(typeof t.id).toBe("number");
      expect(t.day).toBe("2024-06-01");
      expect(typeof t.distance_m).toBe("number");
    }
    const filtered = await api.trips("2024-06-01");
    expect(filtered.length).toBe(3);
    const none = await api.trips("1999-01-01");
    expect(none.length).toBe(0);
  });

  test("trip(:id) returns detail with points; missing id is a JSON 404", async () => {
    const t = await api.trip(1);
    expect(t.id).toBe(1);
    expect(Array.isArray(t.points)).toBe(true);
    // Trip detail returns the full ordered point path. The 5.3 map seed enriches
    // each driving trip into a realistic multi-point route (trip 1 = an 8-point
    // path that also forms the trip1∩trip2 overlap used by route disambiguation),
    // so this asserts a multi-point path rather than the original 2-point stub.
    expect(t.points.length).toBeGreaterThanOrEqual(2);
    expect(t.points[0]).toHaveProperty("lat");
    expect(t.points[0]).toHaveProperty("lon");

    const err = await api.trip(999_999).then(
      () => null,
      (e) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
    expect((err as ApiError).code).toBeTruthy();
    expect((err as ApiError).message).toBeTruthy();
  });

  test("events() returns a cursor page of typed items", async () => {
    const page = await api.events({ limit: 100 });
    expect(Array.isArray(page.items)).toBe(true);
    expect(page.items.length).toBe(3);
    expect(page).toHaveProperty("next_cursor");
    expect(page.limit).toBe(100);
    const ev = page.items[0];
    for (const k of ["id", "type", "severity", "t"]) {
      expect(ev).toHaveProperty(k);
    }
  });

  test("events(trip=) scopes to a trip", async () => {
    const page = await api.events({ trip: 1, limit: 100 });
    expect(page.items.every((e) => e.trip_id === 1)).toBe(true);
    expect(page.items.length).toBe(1);
  });

  test("clips() returns a cursor page; each clip carries its angles", async () => {
    const page = await api.clips({ limit: 500 });
    expect(page.items.length).toBe(30);
    const clip = page.items[0];
    expect(Array.isArray(clip.angles)).toBe(true);
    expect(clip.angles.length).toBe(4);
    const angle = clip.angles[0];
    expect(angle).toHaveProperty("camera");
    // view_kind is an opaque string — must pass through unknown values verbatim.
    expect(typeof angle.view_kind).toBe("string");
  });

  test("clips(folder_class=) filters by folder", async () => {
    const sentry = await api.clips({ folder_class: "SentryClips", limit: 500 });
    expect(sentry.items.length).toBe(10);
    expect(sentry.items.every((c) => c.folder_class === "SentryClips")).toBe(true);
  });

  test("clip(:id) returns one clip; missing id is a JSON 404", async () => {
    const c = await api.clip(1);
    expect(c.id).toBe(1);
    expect(Array.isArray(c.angles)).toBe(true);

    const err = await api.clip(999_999).then(
      () => null,
      (e) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(404);
  });

  test("analytics() returns totals and breakdowns", async () => {
    const a = await api.analytics();
    expect(a.total_trips).toBe(3);
    expect(a.total_events).toBe(3);
    expect(a.total_distance_m).toBeGreaterThan(0);
    expect(Array.isArray(a.events_by_type)).toBe(true);
    expect(a.events_by_type.length).toBe(3); // harsh_braking, sharp_turn, sentry
    expect(Array.isArray(a.trips_by_day)).toBe(true);
    expect(a.trips_by_day[0].day).toBe("2024-06-01");
  });

  test("settings() returns the seeded prefs as k/v pairs", async () => {
    const prefs = await api.settings();
    expect(Array.isArray(prefs)).toBe(true);
    const map = new Map(prefs.map((p) => [p.key, p.value]));
    expect(map.get("speed_units")).toBe("kph");
    expect(map.get("speed_limit_mph")).toBe("75");
    expect(map.get("trip_gap_minutes")).toBe("15");
    expect(map.get("display_timezone")).toBe("America/Los_Angeles");
    expect(map.get("samba_enabled")).toBe("true");
  });
});
