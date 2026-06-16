import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "../api/client";
import { subscribeMediaEvents } from "../api/mediaEvents";
import type {
  ChimeGroup,
  GroupInput,
  LibraryEntry,
  ScheduleInput,
  ScheduleType,
  SchedulerSnapshot,
  StoredSchedule,
} from "../api/types";

/**
 * The Chime Scheduler + Random Chime Groups + Chime Library panels of the Lock
 * Chimes screen (rendered inside {@link Media}). Parity target: the v1 Flask
 * `lock_chimes.html` scheduler/groups/library sections — re-implemented over the
 * B-1 `schedulerd` daemon (webd proxies `/api/chime-scheduler/*`).
 *
 * Everything bootstraps from a single `GET /api/chime-scheduler` snapshot
 * (schedules, groups, random-on-boot mode, the chime library, and the form
 * menus). Mutations forward to `schedulerd`, which owns validation and
 * persistence; on success the snapshot is refetched so the UI always reflects
 * daemon-owned state (never optimistic-only). Failures surface a friendly,
 * dismissible message and keep the form populated so nothing is lost.
 */

const RANDOM = "RANDOM";
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_MS = 45000;

type Status = "loading" | "ready" | "error";

type PendingUpload = {
  filename: string;
  bytes: number;
  token: number;
};

type LibraryRow = LibraryEntry & { pending?: boolean; phase?: "syncing" | "waiting" };

/** A blank schedule form (weekly, enabled, 9:00). */
function blankSchedule(): ScheduleInput {
  return {
    name: "",
    chimeFilename: "",
    scheduleType: "weekly",
    days: [],
    month: 1,
    day: 1,
    holiday: "",
    interval: "",
    hour: 9,
    minute: 0,
    enabled: true,
  };
}

/** A blank group form. */
function blankGroup(): GroupInput {
  return { name: "", description: "", chimes: [] };
}

/** A friendly message for a failed scheduler mutation. */
function failMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.status === 0 || err.code === "network") {
      return "Couldn't reach the device. Check the connection and try again.";
    }
    if (err.status === 503) {
      return "The scheduler service is unavailable right now. Try again shortly.";
    }
    return err.message || fallback;
  }
  return (err as Error)?.message || fallback;
}

/** Short two-digit pad for the time selects. */
function pad2(n: number): string {
  return n.toString().padStart(2, "0");
}

/** Human label for a schedule row's trigger (e.g. "Mon, Fri at 09:00"). */
function describeSchedule(s: StoredSchedule): string {
  const time = `${pad2(s.hour ?? 0)}:${pad2(s.minute ?? 0)}`;
  switch (s.scheduleType) {
    case "weekly": {
      const days = (s.days ?? []).map((d) => d.slice(0, 3)).join(", ");
      return `${days || "No days"} at ${time}`;
    }
    case "date":
      return `${pad2(s.month ?? 1)}/${pad2(s.day ?? 1)} at ${time}`;
    case "holiday":
      return `${s.holiday || "Holiday"} at 00:00`;
    case "recurring":
      return `Rotates: ${s.interval || "?"}`;
    default:
      return time;
  }
}

interface ChimeSchedulerProps {
  pendingUpload?: PendingUpload | null;
  onActivated?: (filename: string, bytes: number) => void;
  onLibraryLoaded?: (library: LibraryEntry[]) => void;
  activationBusy?: boolean;
}

export function ChimeScheduler({
  pendingUpload,
  onActivated,
  onLibraryLoaded,
  activationBusy,
}: ChimeSchedulerProps = {}) {
  const [status, setStatus] = useState<Status>("loading");
  const [snap, setSnap] = useState<SchedulerSnapshot | null>(null);

  // ── Schedule form ──
  const [sForm, setSForm] = useState<ScheduleInput>(blankSchedule());
  const [sEditId, setSEditId] = useState<string | null>(null);
  const [sBusy, setSBusy] = useState(false);
  const [sError, setSError] = useState<string | null>(null);

  // ── Group modal ──
  const [groupOpen, setGroupOpen] = useState(false);
  const [gForm, setGForm] = useState<GroupInput>(blankGroup());
  const [gEditId, setGEditId] = useState<string | null>(null);
  const [gBusy, setGBusy] = useState(false);
  const [gError, setGError] = useState<string | null>(null);

  // ── Random mode ──
  const [randomGroup, setRandomGroup] = useState("");
  const [randomBusy, setRandomBusy] = useState(false);
  const [randomError, setRandomError] = useState<string | null>(null);

  // ── Library row actions (Set Active / Delete) ──
  const [libError, setLibError] = useState<string | null>(null);
  const [libNotice, setLibNotice] = useState<string | null>(null);
  const [activating, setActivating] = useState<string | null>(null);
  const [pending, setPending] = useState<{
    filename: string;
    bytes: number;
    token: number;
    phase: "syncing" | "waiting";
  } | null>(null);
  // ── Pending library deletes (auto-refresh: wait for the row to leave the
  // catalog) ──
  // Each entry carries its OWN `startedAt` so a sibling delete that converges
  // can never reset another row's 45s budget. Known limitation: re-uploading a
  // file with the same name while its delete is still pending can leave the
  // delete stuck in "waiting" (the stale catalog row reappears) — the row's
  // "Refresh now" recovers it; the delete button is disabled on an
  // upload-pending row so the inverse can't be triggered from the table.
  const [pendingDeletes, setPendingDeletes] = useState<
    { filename: string; phase: "removing" | "waiting"; startedAt: number }[]
  >([]);
  const [deleteToken, setDeleteToken] = useState(0);
  const pendingDeletesRef = useRef<
    { filename: string; phase: "removing" | "waiting"; startedAt: number }[]
  >([]);
  // Filenames with an in-flight DELETE request — set synchronously before the
  // await so a rapid double-click can't fire two DELETEs before the row locks.
  const deleteInFlightRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    pendingDeletesRef.current = pendingDeletes;
  }, [pendingDeletes]);

  const reload = async (signal?: AbortSignal): Promise<SchedulerSnapshot | null> => {
    try {
      const s = await api.scheduler(signal);
      setSnap(s);
      setRandomGroup(s.randomMode.groupId ?? "");
      setStatus("ready");
      return s;
    } catch {
      if (!signal?.aborted) setStatus("error");
      return null;
    }
  };

  useEffect(() => {
    const ctrl = new AbortController();
    void reload(ctrl.signal);
    return () => ctrl.abort();
  }, []);

  // Realtime: silently reload the scheduler snapshot (which carries the chime
  // library) whenever webd reports a catalog change, so an installed/removed
  // library chime appears or disappears promptly. The per-op convergence polls
  // below still own the "syncing"/"removing" badge lifecycle.
  const silentReloadRef = useRef<() => void>(() => {});
  silentReloadRef.current = () => {
    void reload();
  };
  useEffect(
    () => subscribeMediaEvents(() => silentReloadRef.current()),
    [],
  );

  // Report the library up to the parent so the "Active Lock Chime" card can
  // resolve which library chime is installed (the car file is always named
  // `LockChime.wav`). `setLibrary` from the parent is referentially stable.
  useEffect(() => {
    if (snap) onLibraryLoaded?.(snap.library);
  }, [snap, onLibraryLoaded]);

  useEffect(() => {
    if (!pendingUpload) {
      setPending(null);
      return;
    }

    let cancelled = false;
    const ctrl = new AbortController();
    let pollId: ReturnType<typeof setTimeout> | null = null;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    const startedAt = Date.now();

    const stopPolling = () => {
      if (pollId) clearTimeout(pollId);
      if (timeoutId) clearTimeout(timeoutId);
      pollId = null;
      timeoutId = null;
    };

    const matchPending = (snapshot: SchedulerSnapshot) =>
      // Convergence key = filename + EXACT byte size. Known trade-off: the
      // catalog exposes no content hash, so re-uploading a *different* WAV with
      // the same name AND byte-identical length confirms early against the stale
      // row. Harmless — the verbatim overwrite still lands the new bytes at the
      // same path; only the "confirmed" notice is slightly premature. Using the
      // `modified` mtime instead would trade this for browser-vs-Pi clock-skew
      // false-negatives (annoying timeouts), so we accept the cosmetic race.
      snapshot.library.some(
        (entry) =>
          entry.filename === pendingUpload.filename && entry.bytes === pendingUpload.bytes,
      );

    const runReload = async () => {
      if (cancelled) return;
      const snapshot = await reload(ctrl.signal);
      if (!snapshot || cancelled) return;
      const matched = matchPending(snapshot);
      if (matched) {
        stopPolling();
        if (!cancelled) {
          setPending(null);
          setLibNotice(`“${pendingUpload.filename}” added to your chime library.`);
        }
        return;
      }
      if (Date.now() - startedAt < POLL_MAX_MS) {
        pollId = setTimeout(() => {
          void runReload();
        }, POLL_INTERVAL_MS);
      }
    };

    setPending({
      filename: pendingUpload.filename,
      bytes: pendingUpload.bytes,
      token: pendingUpload.token,
      phase: "syncing",
    });
    setLibError(null);
    setLibNotice(null);

    void runReload();
    timeoutId = setTimeout(() => {
      if (cancelled) return;
      // Bounded: stop polling AND abort any in-flight GET so a slow Pi response
      // can't race the "waiting" state or a manual Refresh-now after timeout.
      ctrl.abort();
      stopPolling();
      setPending((current) =>
        current && current.token === pendingUpload.token
          ? { ...current, phase: "waiting" }
          : current,
      );
    }, POLL_MAX_MS);

    return () => {
      cancelled = true;
      ctrl.abort();
      stopPolling();
    };
  }, [pendingUpload?.token]);

  useEffect(() => {
    if (deleteToken === 0) return;
    if (!pendingDeletesRef.current.some((d) => d.phase === "removing")) return;

    let cancelled = false;
    const ctrl = new AbortController();
    let pollId: ReturnType<typeof setTimeout> | null = null;

    const stop = () => {
      if (pollId) clearTimeout(pollId);
      pollId = null;
    };

    const runPoll = async () => {
      if (cancelled) return;
      const snapshot = await reload(ctrl.signal);
      if (cancelled) return;
      const now = Date.now();
      // Poll every 2s; each row owns a 45s budget from its own `startedAt`.
      // Budget the last read so the waiting affordance appears before the clock
      // jumps past the cap.
      const isTimedOut = (startedAt: number) =>
        now - startedAt >= POLL_MAX_MS - POLL_INTERVAL_MS;
      const before = pendingDeletesRef.current;

      if (!snapshot) {
        // GET failed/aborted: roll only the rows whose own budget elapsed into
        // the waiting phase; keep polling while any row is still within budget.
        setPendingDeletes((prev) =>
          prev.map((d) =>
            d.phase === "removing" && isTimedOut(d.startedAt) ? { ...d, phase: "waiting" } : d,
          ),
        );
        if (!cancelled && before.some((d) => d.phase === "removing" && !isTimedOut(d.startedAt))) {
          pollId = setTimeout(() => void runPoll(), POLL_INTERVAL_MS);
        } else {
          stop();
        }
        return;
      }

      const present = new Set(snapshot.library.map((entry) => entry.filename));
      const removed = before.filter((d) => !present.has(d.filename)).map((d) => d.filename);

      // Single update: drop rows that left the catalog AND roll budget-elapsed
      // survivors into the waiting phase.
      setPendingDeletes((prev) =>
        prev
          .filter((d) => present.has(d.filename))
          .map((d) =>
            d.phase === "removing" && isTimedOut(d.startedAt) ? { ...d, phase: "waiting" } : d,
          ),
      );
      if (removed.length > 0) {
        setLibNotice(
          removed.length === 1
            ? `“${removed[0]}” removed from your chime library.`
            : `${removed.length} chimes removed from your chime library.`,
        );
      }

      const stillRemoving = before.some(
        (d) => present.has(d.filename) && d.phase === "removing" && !isTimedOut(d.startedAt),
      );
      if (stillRemoving) {
        pollId = setTimeout(() => void runPoll(), POLL_INTERVAL_MS);
      } else {
        stop();
      }
    };

    pollId = setTimeout(() => void runPoll(), POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      ctrl.abort();
      stop();
    };
  }, [deleteToken]);

  // ── Schedule form handlers ──
  function resetScheduleForm() {
    setSForm(blankSchedule());
    setSEditId(null);
    setSError(null);
  }

  function editSchedule(s: StoredSchedule) {
    setSEditId(s.id);
    setSForm({ ...blankSchedule(), ...s });
    setSError(null);
    document
      .getElementById("scheduler-section")
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function toggleDay(day: string) {
    setSForm((f) => {
      const days = new Set(f.days ?? []);
      if (days.has(day)) days.delete(day);
      else days.add(day);
      return { ...f, days: [...days] };
    });
  }

  async function submitSchedule(e: Event) {
    e.preventDefault();
    if (sBusy) return;
    setSBusy(true);
    setSError(null);
    // Recurring schedules always use Random (v1 behavior).
    const payload: ScheduleInput = {
      ...sForm,
      chimeFilename:
        sForm.scheduleType === "recurring" ? RANDOM : sForm.chimeFilename,
    };
    try {
      if (sEditId) await api.updateSchedule(sEditId, payload);
      else await api.addSchedule(payload);
      resetScheduleForm();
      await reload();
    } catch (err) {
      setSError(failMessage(err, "Couldn't save the schedule."));
    } finally {
      setSBusy(false);
    }
  }

  async function removeSchedule(id: string) {
    try {
      await api.deleteSchedule(id);
      if (sEditId === id) resetScheduleForm();
      await reload();
    } catch (err) {
      setSError(failMessage(err, "Couldn't delete the schedule."));
    }
  }

  // ── Group modal handlers ──
  function openCreateGroup() {
    setGEditId(null);
    setGForm(blankGroup());
    setGError(null);
    setGroupOpen(true);
  }

  function openEditGroup(g: ChimeGroup) {
    setGEditId(g.id);
    setGForm({ name: g.name, description: g.description, chimes: [...g.chimes] });
    setGError(null);
    setGroupOpen(true);
  }

  function closeGroup() {
    if (gBusy) return;
    setGroupOpen(false);
  }

  function toggleGroupChime(filename: string) {
    setGForm((f) => {
      const chimes = new Set(f.chimes);
      if (chimes.has(filename)) chimes.delete(filename);
      else chimes.add(filename);
      return { ...f, chimes: [...chimes] };
    });
  }

  async function submitGroup(e: Event) {
    e.preventDefault();
    if (gBusy) return;
    setGBusy(true);
    setGError(null);
    try {
      if (gEditId) await api.updateGroup(gEditId, gForm);
      else await api.addGroup(gForm);
      setGroupOpen(false);
      await reload();
    } catch (err) {
      setGError(failMessage(err, "Couldn't save the group."));
    } finally {
      setGBusy(false);
    }
  }

  async function removeGroup(id: string) {
    try {
      await api.deleteGroup(id);
      await reload();
    } catch (err) {
      setRandomError(failMessage(err, "Couldn't delete the group."));
    }
  }

  // ── Random mode handlers ──
  async function toggleRandomMode() {
    if (randomBusy || !snap) return;
    const enabling = !snap.randomMode.enabled;
    if (enabling && !randomGroup) {
      setRandomError("Select a group first.");
      return;
    }
    setRandomBusy(true);
    setRandomError(null);
    try {
      await api.setRandomMode({
        enabled: enabling,
        groupId: enabling ? randomGroup : null,
      });
      await reload();
    } catch (err) {
      setRandomError(failMessage(err, "Couldn't update random mode."));
    } finally {
      setRandomBusy(false);
    }
  }

  // ── Library handlers ──
  async function setActiveChime(filename: string, bytes: number) {
    setActivating(filename);
    setLibError(null);
    try {
      await api.setActiveChime(filename);
      onActivated?.(filename, bytes);
    } catch (err) {
      setLibError(failMessage(err, "Couldn't set the active chime."));
    } finally {
      setActivating(null);
    }
  }

  async function removeLibraryChime(filename: string) {
    setLibError(null);
    setLibNotice(null);
    if (pendingDeletesRef.current.some((d) => d.filename === filename)) return;
    if (deleteInFlightRef.current.has(filename)) return;
    deleteInFlightRef.current.add(filename);
    try {
      await api.deleteLibraryChime(filename);
    } catch (err) {
      setLibError(failMessage(err, "Couldn't remove the chime."));
      return;
    } finally {
      deleteInFlightRef.current.delete(filename);
    }

    setPendingDeletes((prev) => {
      const next: { filename: string; phase: "removing" | "waiting"; startedAt: number }[] =
        prev.some((d) => d.filename === filename)
          ? prev
          : [...prev, { filename, phase: "removing", startedAt: Date.now() }];
      pendingDeletesRef.current = next;
      return next;
    });
    setDeleteToken((t) => t + 1);
  }

  async function refreshPendingNow() {
    if (!pending) return;
    const snapshot = await reload();
    if (!snapshot) return;
    const matched = snapshot.library.some(
      (entry) => entry.filename === pending.filename && entry.bytes === pending.bytes,
    );
    if (matched) {
      setPending(null);
      setLibNotice(`“${pending.filename}” added to your chime library.`);
    }
  }

  async function refreshDeletesNow() {
    if (pendingDeletesRef.current.length === 0) return;
    const snapshot = await reload();
    if (!snapshot) return;
    const present = new Set(snapshot.library.map((entry) => entry.filename));
    const removed = pendingDeletesRef.current
      .filter((d) => !present.has(d.filename))
      .map((d) => d.filename);
    if (removed.length > 0) {
      setPendingDeletes((prev) => {
        const next: { filename: string; phase: "removing" | "waiting"; startedAt: number }[] =
          prev.filter((d) => present.has(d.filename));
        pendingDeletesRef.current = next;
        return next;
      });
      setLibNotice(
        removed.length === 1
          ? `“${removed[0]}” removed from your chime library.`
          : `${removed.length} chimes removed from your chime library.`,
      );
    }
  }

  // ── Render ──
  if (status === "loading") {
    return (
      <p class="media-pending" data-testid="scheduler-loading">
        Loading the chime scheduler…
      </p>
    );
  }
  if (status === "error" || !snap) {
    return (
      <p class="media-pending" data-testid="scheduler-error">
        The chime scheduler couldn’t be reached just now. It will appear here
        once the scheduler service is available.
      </p>
    );
  }

  const { schedules, groups, randomMode, library, menus } = snap;
  // Plain computation (not a hook): this runs after the early returns above, so a
  // useMemo here would violate the rules of hooks (conditional hook call).
  const displayLibrary: LibraryRow[] = (() => {
    const rows = library.filter((entry) => {
      if (!pending) return true;
      return !(entry.filename === pending.filename && entry.bytes !== pending.bytes);
    });
    const unique: LibraryRow[] = [];
    const seen = new Set<string>();
    for (const entry of rows) {
      if (seen.has(entry.filename)) continue;
      seen.add(entry.filename);
      unique.push(entry);
    }
    const hasMatch = unique.some(
      (entry) => entry.filename === pending?.filename && entry.bytes === pending?.bytes,
    );
    if (pending && !hasMatch) {
      unique.unshift({
        filename: pending.filename,
        bytes: pending.bytes,
        pending: true,
        phase: pending.phase,
      });
    }
    return unique;
  })();
  const isRecurring = sForm.scheduleType === "recurring";
  const showTime =
    sForm.scheduleType === "weekly" || sForm.scheduleType === "date";

  return (
    <div data-testid="chime-scheduler">
      {/* ── Chime Scheduler ── */}
      <details class="settings-section" id="scheduler-section" open>
        <summary>Chime Scheduler</summary>
        <div class="section-content">
          <form
            class="scheduler-form"
            onSubmit={submitSchedule}
            data-testid="schedule-form"
            novalidate
          >
            <div class="scheduler-grid">
              <div class="scheduler-field">
                <label for="schedule-name">Schedule Name</label>
                <input
                  id="schedule-name"
                  type="text"
                  placeholder="e.g., Morning Chime"
                  data-testid="schedule-name"
                  value={sForm.name}
                  onInput={(e) =>
                    setSForm((f) => ({
                      ...f,
                      name: (e.currentTarget as HTMLInputElement).value,
                    }))
                  }
                  required
                />
              </div>

              {!isRecurring && (
                <div class="scheduler-field">
                  <label for="schedule-chime">Chime</label>
                  <select
                    id="schedule-chime"
                    data-testid="schedule-chime"
                    value={sForm.chimeFilename}
                    onChange={(e) =>
                      setSForm((f) => ({
                        ...f,
                        chimeFilename: (e.currentTarget as HTMLSelectElement)
                          .value,
                      }))
                    }
                    required
                  >
                    <option value="">-- Select a chime --</option>
                    <option value={RANDOM}>Random Chime</option>
                    {library.map((c) => (
                      <option key={c.filename} value={c.filename}>
                        {c.filename}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              <div class="scheduler-field">
                <label>Schedule Type</label>
                <div class="scheduler-radios" data-testid="schedule-type">
                  {(
                    [
                      ["weekly", "Days of Week"],
                      ["date", "Specific Date"],
                      ["holiday", "US Holiday"],
                      ["recurring", "Recurring Rotation"],
                    ] as [ScheduleType, string][]
                  ).map(([val, label]) => (
                    <label key={val} class="scheduler-radio">
                      <input
                        type="radio"
                        name="schedule_type"
                        value={val}
                        checked={sForm.scheduleType === val}
                        onChange={() =>
                          setSForm((f) => ({ ...f, scheduleType: val }))
                        }
                      />
                      <span>{label}</span>
                    </label>
                  ))}
                </div>
              </div>

              {sForm.scheduleType === "weekly" && (
                <div class="scheduler-field" data-testid="days-selection">
                  <label>Days</label>
                  <div class="scheduler-radios">
                    {menus.weekdays.map((d) => (
                      <label key={d} class="scheduler-radio">
                        <input
                          type="checkbox"
                          checked={(sForm.days ?? []).includes(d)}
                          onChange={() => toggleDay(d)}
                        />
                        <span>{d.slice(0, 3)}</span>
                      </label>
                    ))}
                  </div>
                </div>
              )}

              {sForm.scheduleType === "date" && (
                <div class="scheduler-field" data-testid="date-selection">
                  <label>Date</label>
                  <div class="scheduler-time">
                    <select
                      aria-label="Month"
                      value={String(sForm.month ?? 1)}
                      onChange={(e) =>
                        setSForm((f) => ({
                          ...f,
                          month: Number(
                            (e.currentTarget as HTMLSelectElement).value,
                          ),
                        }))
                      }
                    >
                      {Array.from({ length: 12 }, (_, i) => i + 1).map((m) => (
                        <option key={m} value={String(m)}>
                          {pad2(m)}
                        </option>
                      ))}
                    </select>
                    <span class="scheduler-time-sep">/</span>
                    <select
                      aria-label="Day"
                      value={String(sForm.day ?? 1)}
                      onChange={(e) =>
                        setSForm((f) => ({
                          ...f,
                          day: Number(
                            (e.currentTarget as HTMLSelectElement).value,
                          ),
                        }))
                      }
                    >
                      {Array.from({ length: 31 }, (_, i) => i + 1).map((d) => (
                        <option key={d} value={String(d)}>
                          {pad2(d)}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              )}

              {sForm.scheduleType === "holiday" && (
                <div class="scheduler-field" data-testid="holiday-selection">
                  <label for="schedule-holiday">Holiday</label>
                  <select
                    id="schedule-holiday"
                    value={sForm.holiday ?? ""}
                    onChange={(e) =>
                      setSForm((f) => ({
                        ...f,
                        holiday: (e.currentTarget as HTMLSelectElement).value,
                      }))
                    }
                    required
                  >
                    <option value="">-- Select a holiday --</option>
                    {menus.holidays.map((h) => (
                      <option key={h} value={h}>
                        {h}
                      </option>
                    ))}
                  </select>
                  <p class="scheduler-hint">
                    Chime plays at 12:00 AM on the holiday.
                  </p>
                </div>
              )}

              {isRecurring && (
                <div class="scheduler-field" data-testid="interval-selection">
                  <label for="schedule-interval">Rotation Frequency</label>
                  <select
                    id="schedule-interval"
                    value={sForm.interval ?? ""}
                    onChange={(e) =>
                      setSForm((f) => ({
                        ...f,
                        interval: (e.currentTarget as HTMLSelectElement).value,
                      }))
                    }
                    required
                  >
                    <option value="">-- Select frequency --</option>
                    {menus.intervals.map((i) => (
                      <option key={i} value={i}>
                        {i}
                      </option>
                    ))}
                  </select>
                  <p class="scheduler-hint">
                    Recurring schedules always use <strong>Random Chime</strong>{" "}
                    and avoid repeating the currently active chime.
                  </p>
                </div>
              )}

              {showTime && (
                <div class="scheduler-field" data-testid="time-selection">
                  <label>Time (24-hour)</label>
                  <div class="scheduler-time">
                    <select
                      aria-label="Hour"
                      data-testid="schedule-hour"
                      value={String(sForm.hour ?? 0)}
                      onChange={(e) =>
                        setSForm((f) => ({
                          ...f,
                          hour: Number(
                            (e.currentTarget as HTMLSelectElement).value,
                          ),
                        }))
                      }
                    >
                      {Array.from({ length: 24 }, (_, i) => i).map((h) => (
                        <option key={h} value={String(h)}>
                          {pad2(h)}
                        </option>
                      ))}
                    </select>
                    <span class="scheduler-time-sep">:</span>
                    <select
                      aria-label="Minute"
                      data-testid="schedule-minute"
                      value={String(sForm.minute ?? 0)}
                      onChange={(e) =>
                        setSForm((f) => ({
                          ...f,
                          minute: Number(
                            (e.currentTarget as HTMLSelectElement).value,
                          ),
                        }))
                      }
                    >
                      {Array.from({ length: 12 }, (_, i) => i * 5).map((m) => (
                        <option key={m} value={String(m)}>
                          {pad2(m)}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              )}

              <div class="scheduler-field">
                <label class="scheduler-inline-check">
                  <input
                    type="checkbox"
                    checked={sForm.enabled}
                    onChange={(e) =>
                      setSForm((f) => ({
                        ...f,
                        enabled: (e.currentTarget as HTMLInputElement).checked,
                      }))
                    }
                  />
                  <span>Enable this schedule immediately</span>
                </label>
              </div>

              {sError && (
                <p class="chime-upload-status fatal" role="alert" data-testid="schedule-error">
                  {sError}
                </p>
              )}

              <div class="scheduler-actions">
                <button
                  type="submit"
                  class="action-btn primary"
                  data-testid="schedule-submit"
                  disabled={sBusy}
                  aria-busy={sBusy ? "true" : "false"}
                >
                  {sEditId ? "Update Schedule" : "Save Schedule"}
                </button>
                {sEditId && (
                  <button
                    type="button"
                    class="action-btn danger"
                    data-testid="schedule-cancel"
                    onClick={resetScheduleForm}
                    disabled={sBusy}
                  >
                    Cancel
                  </button>
                )}
              </div>
            </div>
          </form>

          {/* Existing schedules */}
          {schedules.length === 0 ? (
            <p class="media-pending" data-testid="schedules-empty">
              No schedules configured. Add one above to automatically change
              chimes at specific times.
            </p>
          ) : (
            <ul class="schedule-list" data-testid="schedule-list">
              {schedules.map((s) => (
                <li
                  key={s.id}
                  class="schedule-item"
                  data-testid="schedule-item"
                  data-schedule-id={s.id}
                >
                  <div class="schedule-item-info">
                    <span class="schedule-item-name">
                      {s.name}
                      {!s.enabled && (
                        <span class="schedule-disabled-badge"> (disabled)</span>
                      )}
                    </span>
                    <span class="schedule-item-detail">
                      {s.chimeFilename === RANDOM ? "Random" : s.chimeFilename}
                      {" — "}
                      {describeSchedule(s)}
                    </span>
                  </div>
                  <div class="schedule-item-actions">
                    <button
                      type="button"
                      class="group-btn group-btn-edit"
                      data-testid="schedule-edit"
                      onClick={() => editSchedule(s)}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      class="group-btn group-btn-delete"
                      data-testid="schedule-delete"
                      onClick={() => void removeSchedule(s.id)}
                    >
                      Delete
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          <div class="scheduler-howto">
            <strong>How it works:</strong> Schedules run automatically every
            minute. The most recent matching schedule wins. Chimes change
            without interrupting Tesla recording.
          </div>
        </div>
      </details>

      {/* ── Random Chime Groups ── */}
      <details class="settings-section" id="groups-section" open>
        <summary>Random Chime Groups</summary>
        <div class="section-content">
          <div
            class={`random-mode-section ${randomMode.enabled ? "active" : ""}`}
            data-testid="random-mode-section"
          >
            <div class="random-mode-header">
              <div class="random-mode-info">
                <h3>Random Mode on Boot</h3>
                <span
                  class={`random-mode-status ${randomMode.enabled ? "enabled" : "disabled"}`}
                  data-testid="random-mode-status"
                >
                  {randomMode.enabled ? "Enabled" : "Disabled"}
                </span>
              </div>
              <div class="random-mode-controls">
                <select
                  class="random-group-select"
                  data-testid="random-group-select"
                  value={randomGroup}
                  disabled={randomMode.enabled || randomBusy}
                  onChange={(e) =>
                    setRandomGroup((e.currentTarget as HTMLSelectElement).value)
                  }
                >
                  <option value="">-- Select a group --</option>
                  {groups.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  class="group-btn group-btn-edit"
                  data-testid="random-mode-toggle"
                  onClick={() => void toggleRandomMode()}
                  disabled={randomBusy}
                >
                  {randomMode.enabled ? "Disable" : "Enable"}
                </button>
              </div>
            </div>
            <div class="random-mode-description">
              <strong>How it works:</strong> when enabled, a random chime from
              the selected group is chosen each time the device boots, avoiding
              the previously selected chime.
            </div>
            {randomError && (
              <p class="chime-upload-status fatal" role="alert" data-testid="random-error">
                {randomError}
              </p>
            )}
          </div>

          <div class="scheduler-actions" style="margin: 20px 0;">
            <button
              type="button"
              class="action-btn"
              data-testid="create-group"
              onClick={openCreateGroup}
            >
              Create New Group
            </button>
          </div>

          <div class="groups-container" data-testid="groups-container">
            {groups.length === 0 ? (
              <div class="group-empty-state" data-testid="groups-empty">
                <p>
                  No chime groups yet. Create a group to organize your lock
                  chimes!
                </p>
              </div>
            ) : (
              groups.map((g) => (
                <div
                  key={g.id}
                  class={`group-card ${randomMode.enabled && randomMode.groupId === g.id ? "active-random-group" : ""}`}
                  data-testid="group-card"
                  data-group-id={g.id}
                >
                  <div class="group-header">
                    <div class="group-info">
                      <h3 class="group-name">{g.name}</h3>
                      {g.description && (
                        <p class="group-description">{g.description}</p>
                      )}
                      <div class="group-meta">
                        <span class="group-meta-item">
                          {g.chimes.length} chime
                          {g.chimes.length === 1 ? "" : "s"}
                        </span>
                      </div>
                    </div>
                    <div class="group-actions">
                      <button
                        type="button"
                        class="group-btn group-btn-edit"
                        data-testid="group-edit"
                        onClick={() => openEditGroup(g)}
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        class="group-btn group-btn-delete"
                        data-testid="group-delete"
                        onClick={() => void removeGroup(g.id)}
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                  {g.chimes.length > 0 && (
                    <div class="group-chimes-list">
                      <div class="group-chimes">
                        {g.chimes.map((c) => (
                          <span key={c} class="chime-tag">
                            {c}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      </details>

      {/* ── Chime Library ── */}
      <details class="settings-section" id="library-section" open>
        <summary>Chime Library</summary>
        <div class="section-content">
          {libError && (
            <p class="chime-upload-status fatal" role="alert" data-testid="library-error">
              {libError}
            </p>
          )}
          {libNotice && (
            <p class="chime-upload-status success" role="status" data-testid="library-notice">
              {libNotice}
            </p>
          )}

          {displayLibrary.length === 0 ? (
            <p class="media-pending" data-testid="library-empty">
              The chime library is empty. Upload a WAV with “Upload New Chime”
              above and it will appear here.
            </p>
          ) : (
            <table class="chime-library" data-testid="library-table">
              <thead>
                <tr>
                  <th>Filename</th>
                  <th>Size</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {displayLibrary.map((c) => {
                  const pendingRow = Boolean((c as LibraryRow).pending);
                  const deletePhase = pendingDeletes.find((d) => d.filename === c.filename)?.phase;
                  const deleting = deletePhase !== undefined;
                  const rowLocked = pendingRow || deleting;
                  const statusLabel = pendingRow
                    ? c.phase === "waiting"
                      ? "Waiting for media scan…"
                      : "Syncing…"
                    : deleting
                      ? deletePhase === "waiting"
                        ? "Removing — waiting for scan…"
                        : "Removing…"
                      : "Valid";
                  const pendingStatusTestId = pendingRow ? "library-pending-status" : undefined;
                  return (
                    <tr
                      key={c.filename}
                      data-testid={
                        pendingRow
                          ? "library-row library-row-pending"
                          : deleting
                            ? "library-row library-row-deleting"
                            : "library-row"
                      }
                    >
                      <td class="chime-cell-name">{c.filename}</td>
                      <td>{Math.max(1, Math.round(c.bytes / 1024))} KB</td>
                      <td data-testid={pendingStatusTestId}>
                        <span class={pendingRow || deleting ? "chime-status-pending" : "chime-status-valid"}>
                          {statusLabel}
                        </span>
                      </td>
                      <td>
                        <div class="chime-row-actions">
                          <audio
                            controls={!rowLocked}
                            preload="none"
                            data-testid="library-audio"
                            src={api.libraryAudioUrl(c.filename)}
                            aria-disabled={rowLocked ? "true" : undefined}
                          />
                          <div class="chime-row-buttons">
                            <a
                              class="action-btn"
                              data-testid="library-download"
                              href={rowLocked ? undefined : api.libraryDownloadUrl(c.filename)}
                              aria-disabled={rowLocked ? "true" : undefined}
                              onClick={rowLocked ? (e: Event) => e.preventDefault() : undefined}
                            >
                              Download
                            </a>
                            <button
                              type="button"
                              class="action-btn primary"
                              data-testid="library-set-active"
                              disabled={rowLocked || activating !== null || activationBusy}
                              onClick={() => void setActiveChime(c.filename, c.bytes)}
                            >
                              {activating === c.filename ? "Syncing…" : "Set Active"}
                            </button>
                            <button
                              type="button"
                              class="action-btn danger"
                              data-testid="library-delete"
                              disabled={rowLocked}
                              onClick={() => void removeLibraryChime(c.filename)}
                            >
                              {deleting ? "Removing…" : "Delete"}
                            </button>
                          </div>
                          {pendingRow && c.phase === "waiting" && (
                            <button
                              type="button"
                              class="action-btn"
                              data-testid="library-refresh-now"
                              onClick={() => void refreshPendingNow()}
                            >
                              Refresh now
                            </button>
                          )}
                          {deleting && deletePhase === "waiting" && (
                            <button
                              type="button"
                              class="action-btn"
                              data-testid="library-delete-refresh-now"
                              onClick={() => void refreshDeletesNow()}
                            >
                              Refresh now
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </details>

      {/* ── Group create/edit modal ── */}
      {groupOpen && (
        <div
          class="group-modal show"
          role="presentation"
          data-testid="group-modal"
          onClick={closeGroup}
        >
          <div
            class="group-modal-content"
            role="dialog"
            aria-modal="true"
            aria-labelledby="group-modal-title"
            onClick={(e: Event) => e.stopPropagation()}
          >
            <div class="group-modal-header">
              <h3 id="group-modal-title">
                {gEditId ? "Edit Group" : "Create New Group"}
              </h3>
              <span
                class="group-modal-close"
                role="button"
                tabIndex={0}
                data-testid="group-modal-close"
                onClick={closeGroup}
              >
                ×
              </span>
            </div>
            <form onSubmit={submitGroup} novalidate>
              <div class="group-form-field">
                <label for="group-name-input">Group Name *</label>
                <input
                  id="group-name-input"
                  type="text"
                  placeholder="e.g., Holiday Chimes"
                  data-testid="group-name"
                  value={gForm.name}
                  onInput={(e) =>
                    setGForm((f) => ({
                      ...f,
                      name: (e.currentTarget as HTMLInputElement).value,
                    }))
                  }
                  required
                />
              </div>
              <div class="group-form-field">
                <label for="group-desc-input">Description (optional)</label>
                <textarea
                  id="group-desc-input"
                  placeholder="Describe what chimes belong in this group…"
                  data-testid="group-description"
                  value={gForm.description}
                  onInput={(e) =>
                    setGForm((f) => ({
                      ...f,
                      description: (e.currentTarget as HTMLTextAreaElement)
                        .value,
                    }))
                  }
                />
              </div>
              <div class="chime-selector-section">
                <h4>Add Chimes to Group:</h4>
                {library.length === 0 ? (
                  <p class="scheduler-hint">
                    Upload chimes to the library first, then add them here.
                  </p>
                ) : (
                  <div class="chime-checkboxes" data-testid="group-chime-list">
                    {library.map((c) => (
                      <div key={c.filename} class="chime-checkbox-item">
                        <input
                          type="checkbox"
                          id={`gc-${c.filename}`}
                          checked={gForm.chimes.includes(c.filename)}
                          onChange={() => toggleGroupChime(c.filename)}
                        />
                        <label for={`gc-${c.filename}`}>{c.filename}</label>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              {gError && (
                <p class="chime-upload-status fatal" role="alert" data-testid="group-error">
                  {gError}
                </p>
              )}
              <div class="group-form-actions">
                <button
                  type="button"
                  class="group-btn"
                  data-testid="group-cancel"
                  onClick={closeGroup}
                  disabled={gBusy}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  class="group-btn group-btn-edit"
                  data-testid="group-save"
                  disabled={gBusy}
                  aria-busy={gBusy ? "true" : "false"}
                >
                  Save Group
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
