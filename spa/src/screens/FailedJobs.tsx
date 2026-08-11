import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { Icon } from "../components/Icon";
import { ApiError, api } from "../api/client";
import type { FailedJob, FailedUploadHistoryItem } from "../api/types";
import "../styles/failed-jobs.css";

/**
 * Failed jobs screen (route `/failed-jobs`, Shell active "settings") — a
 * read-only operator-triage view of the background jobs `webd` has retained as
 * FAILED (parity `failed_jobs.html`, contract D2 `webd-api.md` §2.1/§3).
 *
 * Data boundary: webd is **read-only**. This screen issues only read GETs and
 * never mutates: `GET /api/jobs/failed` plus
 * `GET /api/jobs/failed/uploads?limit=...`. The jobs snapshot is a WRAPPED
 * `{ "jobs": JobStatus[] }` ring (verified against
 * rust/crates/webd/src/route.rs `jobs_failed`), bounded at 100 retained
 * failures. A `JobStatus` is `{ job_id, kind, state, progress, detail?,
 * handoff_id? }` (jobs.rs); there is no timestamp field, so this screen does
 * not invent one. The cloud failed-upload endpoint is durable history from
 * `indexd` and *does* carry `at` timestamps.
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

/**
 * Server-side retention cap on the failed-job ring (`MAX_FAILED_RETAINED` in
 * webd jobs.rs). At this length older failures may have been evicted, so the
 * UI notes the snapshot is bounded.
 */
const RING_CAP = 100;
const FAILED_UPLOAD_HISTORY_LIMIT = 16;

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | {
      status: "ready";
      jobs: FailedJob[];
      uploadFailures: FailedUploadHistoryItem[];
      uploadFailuresError: string | null;
    };

/**
 * Fetch the failed-jobs snapshot through the shared typed API client.
 */
async function fetchFailedJobs(signal?: AbortSignal): Promise<FailedJob[]> {
  const response = await api.failedJobs(signal);
  if (!Array.isArray(response.jobs)) {
    throw new ApiError(200, "bad_shape", "unexpected response shape");
  }
  return response.jobs;
}

async function fetchFailedUploads(signal?: AbortSignal): Promise<FailedUploadHistoryItem[]> {
  const response = await api.cloudFailedUploadHistory(
    { limit: FAILED_UPLOAD_HISTORY_LIMIT },
    signal,
  );
  if (!Array.isArray(response.items)) {
    throw new ApiError(200, "bad_shape", "unexpected response shape");
  }
  return response.items;
}

/** Fractional progress (0..1) → a clamped percent, or "—" when unknown. */
function progressText(progress: number | null | undefined): string {
  if (progress == null || !Number.isFinite(progress)) return DASH;
  const pct = Math.min(100, Math.max(0, progress * 100));
  return `${Math.round(pct)}%`;
}

function formatAt(at: number): string {
  if (!Number.isFinite(at)) return DASH;
  return new Date(at * 1000).toLocaleString();
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

function UploadFailureItem({ item }: { item: FailedUploadHistoryItem }) {
  return (
    <article class="fj-item fj-upload-item">
      <div class="fj-item-head">
        <span class="fj-kind">cloud_upload</span>
        <span class="fj-badge" data-severity="error">
          <span class="fj-dot" aria-hidden="true" />
          Failed
        </span>
      </div>
      <dl class="fj-dl">
        <dt>Archive item</dt>
        <dd class="fj-mono">{item.archive_item_id}</dd>
        <dt>Child key</dt>
        <dd class="fj-mono fj-wrap">{item.child_key || DASH}</dd>
        <dt>When</dt>
        <dd>{formatAt(item.at)}</dd>
        <dt>Bytes</dt>
        <dd>{item.size_bytes}</dd>
        <dt>Error class</dt>
        <dd class="fj-wrap">{item.error_class || DASH}</dd>
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
    Promise.allSettled([fetchFailedJobs(ctrl.signal), fetchFailedUploads(ctrl.signal)])
      .then(([jobsResult, uploadResult]) => {
        if (!mounted.current || seq !== reqSeq.current) return;
        if (jobsResult.status === "rejected") {
          if (ctrl.signal.aborted) return;
          const err = jobsResult.reason;
          const message =
            err instanceof ApiError && err.message
              ? err.message
              : "Could not load failed jobs.";
          setState({ status: "error", message });
          return;
        }
        const uploadFailuresError =
          uploadResult.status === "rejected"
            ? uploadResult.reason instanceof ApiError &&
              uploadResult.reason.message
              ? uploadResult.reason.message
              : "Could not load failed upload history."
            : null;
        setState({
          status: "ready",
          jobs: jobsResult.value,
          uploadFailures:
            uploadResult.status === "fulfilled" ? uploadResult.value : [],
          uploadFailuresError,
        });
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
  const uploadFailures = state.status === "ready" ? state.uploadFailures : [];
  const uploadFailuresError =
    state.status === "ready" ? state.uploadFailuresError : null;
  const atCap = state.status === "ready" && state.jobs.length >= RING_CAP;

  const statusLine =
    state.status === "loading"
      ? "Loading failed jobs…"
      : state.status === "error"
        ? "Couldn't load failed jobs."
        : jobs.length === 0
          ? uploadFailures.length > 0
            ? "No retained in-memory failures."
            : "No failed jobs."
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
          <p class="fj-empty-title">
            {uploadFailures.length > 0 ? "No retained in-memory failures" : "No failed jobs"}
          </p>
          <p class="fj-empty-copy">
            {uploadFailures.length > 0
              ? "Durable failed cloud uploads, if any, are listed below."
              : "Every retained in-memory job has completed without error."}
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

      {state.status === "ready" ? (
        <section class="fj-card fj-upload-history" data-testid="failed-upload-history">
          <h2 class="fj-subtitle">Failed cloud uploads</h2>
          <p
            class="fj-status"
            data-testid="failed-upload-history-status"
            role="status"
            aria-live="polite"
          >
            {uploadFailuresError
              ? "Failed-upload history is unavailable."
              : uploadFailures.length === 0
                ? "No failed cloud uploads."
                : `${uploadFailures.length} failed cloud upload${uploadFailures.length === 1 ? "" : "s"}.`}
          </p>
          {uploadFailuresError ? (
            <p class="fj-note" data-testid="failed-upload-history-error">
              {uploadFailuresError}
            </p>
          ) : uploadFailures.length === 0 ? (
            <p class="fj-note" data-testid="failed-upload-history-empty">
              No durable failed-upload rows were returned.
            </p>
          ) : (
            <div class="fj-list" data-testid="failed-upload-history-list">
              {uploadFailures.map((item) => (
                <UploadFailureItem
                  key={`${item.archive_item_id}-${item.child_key}-${item.at}`}
                  item={item}
                />
              ))}
            </div>
          )}
        </section>
      ) : null}
    </section>
  );
}
