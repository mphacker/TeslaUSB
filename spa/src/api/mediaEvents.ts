/**
 * Singleton client for the webd media-change push stream
 * (`GET /api/media-events`, a Server-Sent Events endpoint).
 *
 * webd runs a background monitor over SQLite's `PRAGMA data_version` and emits a
 * `media-changed` event whenever `indexd` commits a catalog change (a media
 * install/delete that has been applied and indexed). Media screens subscribe
 * here and refetch their current list on each tick, replacing the old
 * per-screen polling timers with near-instant updates.
 *
 * One `EventSource` is shared by all subscribers in the tab. It is opened lazily
 * on the first subscription and closed when the last subscriber leaves, so
 * screens that never mount cost nothing. `EventSource` reconnects automatically
 * if the connection drops. Ticks are coalesced through a short debounce so a
 * burst of catalog commits triggers at most one refetch per screen.
 */

const STREAM_PATH = "/api/media-events";
const EVENT_NAME = "media-changed";

/** Debounce window: collapse a burst of catalog commits into one refetch. */
const DEBOUNCE_MS = 150;

type Listener = () => void;

const listeners = new Set<Listener>();
let source: EventSource | null = null;
let debounceTimer: ReturnType<typeof setTimeout> | null = null;

function notify(): void {
  if (debounceTimer !== null) return;
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
    for (const fn of [...listeners]) {
      try {
        fn();
      } catch {
        // A listener throwing must not break delivery to the others.
      }
    }
  }, DEBOUNCE_MS);
}

function open(): void {
  if (source !== null) return;
  // Guard environments without EventSource (e.g. jsdom under unit tests):
  // subscribers still register, they just never receive a server tick.
  if (typeof EventSource === "undefined") return;
  source = new EventSource(STREAM_PATH, { withCredentials: true });
  source.addEventListener(EVENT_NAME, () => notify());
}

function close(): void {
  if (source !== null) {
    source.close();
    source = null;
  }
  if (debounceTimer !== null) {
    clearTimeout(debounceTimer);
    debounceTimer = null;
  }
}

/**
 * Subscribe to media-change ticks. The callback fires (debounced) whenever the
 * catalog changes server-side. Returns an unsubscribe function; the shared
 * stream is closed once the last subscriber unsubscribes.
 */
export function subscribeMediaEvents(onChange: Listener): () => void {
  listeners.add(onChange);
  open();
  return () => {
    listeners.delete(onChange);
    if (listeners.size === 0) close();
  };
}
