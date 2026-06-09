import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { Icon } from "../components/Icon";
import { ApiError } from "../api/client";
import "../styles/failed-jobs.css";

/**
 * Failed jobs screen (route `/failed-jobs`, Shell active "settings") — a
 * read-only operator-triage view of the background jobs `webd` has retained as
 * FAILED (parity `failed_jobs.html`, contract D2 `webd-api.md` §2.1/§3).
 *
 * Data boundary: webd is **read-only**. This screen issues exactly ONE GET —
 * `GET /api/jobs/failed` — and never mutates. The realized contract returns a
 * WRAPPED snapshot `{ "jobs": JobStatus[] }` (verified against
 * rust/crates/webd/src/route.rs `jobs_failed`), a bounded ring of at most 100
 * retained failures. A `JobStatus` is
 * `{ job_id, kind, state, progress, detail?, handoff_id? }` (jobs.rs); there is
 * NO timestamp field, so this screen does not invent one.
 *
 * Ordering: the ring is returned OLDEST-first (a FIFO `VecDeque`, eviction at
 * the head). Insertion order is the true failure-retention order, so we render
 * `.reverse()` (newest failure first) rather than sorting by `job_id` —
 * `job_id` is process-monotonic *creation* order, not failure order, and a
 * long-running older job can fail after a newer one.
 *
 * Live updates: the optional SSE stream (`GET /api/jobs`) is intentionally NOT
 * used. An open `EventSource` is a long-lived connection (it would defeat the
 * UAT's `networkidle` settle and risk reconnect console noise against the
 * zero-console gate), and a bounded failed ring is well served by an explicit
 * Refresh. The contract permits the REST snapshot to stand alone.
 *
 * Robustness: each load uses an `AbortController` + a request-sequence guard so
 * rapid Refresh clicks or an unmount can never let a stale response overwrite a
 * newer one or update after teardown; every failure path resolves to a handled
 * `error` state (never a render-time throw), keeping the zero-console gate.
 */

const DASH = "\u2014";

/** Path of the single read-only endpoint this screen consumes. */
const FAILED_JOBS_PATH = "/api/jobs/failed";

/**
 * Server-side retention cap on the failed-job ring (`MAX_FAILED_RETAINED` in
 * webd jobs.rs). At this length older failures may have been evicted, so the
 * UI notes the snapshot is bounded.
 */
const RING_CAP = 100;

/** Job lifecycle states (contract §3). Kept as a closed union for the badge,
 *  but the wire value is treated opaquely — an unknown future state degrades to
 *  a neutral badge rather than being rejected. */
type JobState = "running" | "done" | "failed" | "refused" | "busy";

/** One retained job (the realized `job_status` payload, webd jobs.rs). */
interface FailedJob {
  job_id: number;
  kind: string;
  state: JobState | string;
  progress: number | null;
  detail?: string;
  handoff_id?: string;
}

/** The realized `GET /api/jobs/failed` envelope. */
interface FailedJobsResponse {
  jobs: FailedJob[];
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; jobs: FailedJob[] };

/**
 * Fetch the failed-jobs snapshot. Mirrors the shared catalog client's
 * `getJson` runtime behaviour (same-origin credentials, `Accept: json`,
 * text-then-parse, `ApiError` envelope) so this screen behaves identically to
 * the typed `api` client without editing that shared module. Defensively
 * validates the `{ jobs: [...] }` wrapper: an unexpected shape becomes a
 * handled error, never a render-time exception.
 */
async function fetchFailedJobs(signal?: AbortSignal): Promise<FailedJob[]> {
  let resp: Response;
  try {
    resp = await fetch(FAILED_JOBS_PATH, {
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
      if (!resp.ok) throw new ApiError(resp.status, "http_error", `HTTP ${resp.status}`);
      throw new ApiError(resp.status, "bad_json", "malformed JSON response");
    }
  }
  if (!resp.ok) {
    const env = body as { error?: { code?: string; message?: string } } | null;
    throw new ApiError(
      resp.status,
      env?.error?.code ?? "http_error",
      env?.error?.message ?? `HTTP ${resp.status}`,
    );
  }
  const jobs = (body as FailedJobsResponse | null)?.jobs;
  if (!Array.isArray(jobs)) {
    throw new ApiError(resp.status, "bad_shape", "unexpected response shape");
  }
  return jobs as FailedJob[];
}

/** Fractional progress (0..1) → a clamped percent, or "—" when unknown. */
function progressText(progress: number | null | undefined): string {
  if (progress == null || !Number.isFinite(progress)) return DASH;
  const pct = Math.min(100, Math.max(0, progress * 100));
  return `${Math.round(pct)}%`;
}

/** A single failed-job card. */
function JobItem({ job }: { job: FailedJob }) {
  return (
    <article class="fj-item" data-job-id={String(job.job_id)} data-state={job.state}>
      <div class="fj-item-head">
        <span class="fj-kind">{job.kind || DASH}</span>
        <span class="fj-badge" data-severity="error">
          <span class="fj-dot" aria-hidden="true" />
          Failed
        </span>
      </div>
      <dl class="fj-dl">
        <dt>Job ID</dt>
        <dd class="fj-mono">{String(job.job_id)}</dd>
        <dt>Progress</dt>
        <dd>{progressText(job.progress)}</dd>
        {job.handoff_id ? (
          <>
            <dt>Handoff</dt>
            <dd class="fj-mono fj-wrap">{job.handoff_id}</dd>
          </>
        ) : null}
        {job.detail ? (
          <>
            <dt>Detail</dt>
            <dd class="fj-detail fj-wrap">{job.detail}</dd>
          </>
        ) : null}
      </dl>
    </article>
  );
}

export function FailedJobs() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const reqSeq = useRef(0);
  const mounted = useRef(true);
  const abortRef = useRef<AbortController | null>(null);

  const load = useCallback(() => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const seq = ++reqSeq.current;
    setState({ status: "loading" });
    fetchFailedJobs(ctrl.signal)
      .then((jobs) => {
        if (!mounted.current || seq !== reqSeq.current) return;
        setState({ status: "ready", jobs });
      })
      .catch((err) => {
        // A superseded/aborted request is not an error the user should see.
        if (!mounted.current || seq !== reqSeq.current || ctrl.signal.aborted) return;
        const message =
          err instanceof ApiError && err.message
            ? err.message
            : "Could not load failed jobs.";
        setState({ status: "error", message });
      });
  }, []);

  useEffect(() => {
    mounted.current = true;
    load();
    return () => {
      mounted.current = false;
      abortRef.current?.abort();
    };
  }, [load]);

  const loading = state.status === "loading";
  // Newest failure first = reverse of the oldest-first ring. Defensive filter to
  // `failed` keeps a future backend change from leaking other states here.
  const jobs =
    state.status === "ready"
      ? state.jobs.filter((j) => j.state === "failed").reverse()
      : [];
  const atCap = state.status === "ready" && state.jobs.length >= RING_CAP;

  const statusLine =
    state.status === "loading"
      ? "Loading failed jobs…"
      : state.status === "error"
        ? "Couldn't load failed jobs."
        : jobs.length === 0
          ? "No failed jobs."
          : `${jobs.length} failed job${jobs.length === 1 ? "" : "s"}.`;

  return (
    <section class="failed-jobs-page container" data-screen="failed-jobs">
      <header class="fj-header">
        <div class="fj-header-row">
          <h1 class="fj-title">Failed jobs</h1>
          <button
            type="button"
            class="fj-refresh"
            data-testid="fj-refresh"
            onClick={load}
            disabled={loading}
            aria-label="Refresh failed jobs"
          >
            <Icon name="refresh-cw" class={`fj-refresh-icon${loading ? " fj-spin" : ""}`} />
            <span>Refresh</span>
          </button>
        </div>
        <p class="fj-copy">
          Background jobs that did not complete. This is a read-only snapshot of
          the most recent retained failures — use the detail to triage, then
          retry the original action from its own screen. The snapshot holds up to
          the {RING_CAP} most recent failures; it has no live stream, so use
          Refresh to re-check.
        </p>
        <p class="fj-status" data-testid="fj-status" role="status" aria-live="polite">
          {statusLine}
        </p>
      </header>

      {state.status === "loading" ? (
        <div class="fj-card fj-note" data-testid="failed-jobs-loading">
          Loading failed jobs…
        </div>
      ) : state.status === "error" ? (
        <div class="fj-card fj-error" data-testid="failed-jobs-error">
          <div class="fj-error-head">
            <Icon name="alert-triangle" class="fj-error-icon" />
            <span>{state.message}</span>
          </div>
          <button
            type="button"
            class="fj-retry"
            data-testid="fj-retry"
            onClick={load}
          >
            <Icon name="refresh-cw" class="fj-refresh-icon" />
            <span>Retry</span>
          </button>
        </div>
      ) : jobs.length === 0 ? (
        <div class="fj-card fj-empty" data-testid="failed-jobs-empty">
          <Icon name="check-circle" class="fj-empty-icon" />
          <p class="fj-empty-title">No failed jobs</p>
          <p class="fj-empty-copy">
            Every background job has completed without error.
          </p>
        </div>
      ) : (
        <div class="fj-list" data-testid="failed-jobs-list">
          {atCap ? (
            <p class="fj-cap-note" data-testid="failed-jobs-cap">
              Showing up to the {RING_CAP} most recent retained failures; older
              failures may have been dropped.
            </p>
          ) : null}
          {jobs.map((job, idx) => (
            <JobItem key={`${job.job_id}-${idx}`} job={job} />
          ))}
        </div>
      )}
    </section>
  );
}
