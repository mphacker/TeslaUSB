"""Phase 2b/3a compatibility shim for the queue-driven archive subsystem.

The legacy timer-based ``video_archive_service`` was a ~900 LOC module
that owned three responsibilities:

1. Periodically scanning ``RecentClips/`` and copying new files to
   ``ArchivedClips/``.
2. A "smart cleanup" + "proactive retention" + "enforce retention" trio
   that decided which archived files to delete and when.
3. Path-fixup helpers used by the timer loop after each copy.

All three responsibilities now live elsewhere (Phase 2b + Phase 3a):

* **Archiving** — ``services.archive_worker`` (queue-driven, single
  worker thread, dead-letter aware). Producers enqueue via
  ``archive_queue.enqueue_for_archive`` from ``file_watcher_service`` /
  ``mapping_service`` boot scan; the worker drains one file at a time
  with stable-write gating, atomic copy, throttling, and recovery.

* **Retention** — ``services.archive_watchdog`` is the single
  authoritative retention system. ``archive_watchdog.force_prune_now``
  is the synchronous entry point and is exposed at
  ``POST /api/archive/prune_now``. It honors the
  ``cloud_archive.delete_unsynced`` toggle (Phase 1 #95), the
  ``_retention_running`` duplicate-trigger guard (Phase 3a / #91), and
  the "trips are sacred" invariant — it deletes only ``indexed_files``
  rows and NULLs the ``waypoints.video_path`` / ``detected_events.video_path``
  pointer. Existing ``trips`` / ``waypoints`` / ``detected_events``
  records are preserved.

* **Path fixup** — Replaced by re-enqueueing the archived file into
  ``indexing_queue`` (``mapping_service.enqueue_for_indexing``); the
  indexer rewrites the canonical path on its next pass and
  ``purge_deleted_videos`` removes the stale USB-side row.

This module is kept for backwards compatibility with the public API
that callers still depend on:

* ``start_archive_timer()`` / ``stop_archive_timer()`` —
  ``web_control.startup`` / shutdown handlers; delegate to the worker.
* ``trigger_archive_now()`` — ``POST /api/recent_archive/trigger``
  (the NM dispatcher's WiFi-up entry point) and ``base.html`` /
  ``index.html`` UI buttons. Wakes the worker.
* ``get_archive_status()`` — ``GET /api/recent_archive/status``
  (polled by the NM dispatcher to know when archiving has drained
  before triggering cloud sync). Bridges to ``archive_worker.get_status``.
* ``trigger_archive_cleanup()`` — ``POST /api/archive_cleanup``
  legacy endpoint; one-line wrapper for
  ``archive_watchdog.force_prune_now``. New callers should use
  ``POST /api/archive/prune_now`` directly.

This module should NOT grow. Anything new belongs in ``archive_worker``,
``archive_watchdog``, or ``archive_queue``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from config import ARCHIVE_ENABLED

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
#
# The Phase 2b worker is the single archive thread. ``_archive_cancel``
# is kept as a no-op compatibility flag for any caller that still
# checks it (defensive abort signal). The worker has its own
# ``_stop_event`` and ignores this flag.
_archive_cancel = threading.Event()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_archive_status() -> Dict[str, Any]:
    """Return current archive status — bridge to ``archive_worker.get_status``.

    The dispatcher (``helpers/refresh_cloud_token.py``) polls this
    endpoint and waits for ``running == False`` before kicking cloud
    sync. In the queue-driven architecture the worker is always
    "alive" once started, so ``running`` is True iff there is work
    in flight (an active file being copied, or pending rows in
    the queue).

    Returns a dict with the legacy field names the dispatcher and UI
    code paths expect, augmented with the worker's structured
    snapshot for any new caller that wants more detail.
    """
    legacy: Dict[str, Any] = {
        "running": False,
        "current_file": "",
        "queue_depth": 0,
        "worker_running": False,
        "last_outcome": None,
        "error": None,
    }
    try:
        from services import archive_worker
        snap = archive_worker.get_status()
    except Exception as e:  # noqa: BLE001 — status must never raise
        logger.debug("get_archive_status: archive_worker unavailable: %s", e)
        legacy["error"] = str(e)
        return legacy

    queue_depth = int(snap.get("queue_depth", 0) or 0)
    active_file = snap.get("active_file") or ""
    worker_running = bool(snap.get("worker_running"))
    legacy.update({
        "running": bool(worker_running and (active_file or queue_depth > 0)),
        "current_file": active_file,
        "queue_depth": queue_depth,
        "worker_running": worker_running,
        "last_outcome": snap.get("last_outcome"),
        "claimed_count": int(snap.get("claimed_count", 0) or 0),
        "dead_letter_count": int(snap.get("dead_letter_count", 0) or 0),
        "copied_count": int(snap.get("copied_count", 0) or 0),
        "paused": bool(snap.get("paused")),
        "idle": bool(snap.get("idle")),
    })
    return legacy


def start_archive_timer() -> None:
    """Phase 2b shim: ensure the archive worker is running.

    Callers in ``web_control.startup`` rely on this being a no-op when
    ``ARCHIVE_ENABLED`` is False. A worker startup failure is logged
    but never raised — gadget_web's main thread must not crash on a
    background-subsystem error.
    """
    if not ARCHIVE_ENABLED:
        logger.info("RecentClips archive is disabled in config")
        return
    try:
        from services import archive_worker
        archive_worker.ensure_worker_started()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "archive_worker.ensure_worker_started failed: %s", e,
        )


def stop_archive_timer() -> None:
    """Phase 2b shim: stop the archive worker on service shutdown."""
    _archive_cancel.set()
    try:
        from services import archive_worker
        archive_worker.stop_worker()
    except Exception as e:  # noqa: BLE001
        logger.debug("archive_worker.stop_worker raised: %s", e)


def trigger_archive_now() -> bool:
    """Wake the archive worker — used by NM dispatcher + UI buttons.

    Returns True if the worker was woken (``ARCHIVE_ENABLED`` is True
    and the worker accepted the call), False if archiving is disabled
    or the worker raised. Worker-side exceptions MUST NOT propagate
    — the dispatcher treats False as "no archive in flight" and
    moves on to the cloud-sync trigger.
    """
    if not ARCHIVE_ENABLED:
        return False
    try:
        from services import archive_worker
        archive_worker.ensure_worker_started()
        archive_worker.wake()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("trigger_archive_now: failed to wake worker: %s", e)
        return False


def trigger_archive_cleanup() -> Dict[str, Any]:
    """Phase 3a (#98 / closes #91): one-line wrapper for ``force_prune_now``.

    Backwards-compatible entry point for ``POST /api/archive_cleanup``
    that previously called the legacy ``smart_cleanup_archive`` /
    ``_proactive_retention`` / ``_enforce_retention`` cascade. All
    three of those are gone — retention is owned by
    ``services.archive_watchdog`` and the synchronous entry point is
    ``archive_watchdog.force_prune_now``.

    The watchdog's ``_retention_running`` guard means this call
    returns immediately (with ``status='already_running'``) if a prune
    is already in flight, so the request thread no longer block-waits
    up to 60 s on the ``task_coordinator`` 'retention' slot.

    New callers should use ``POST /api/archive/prune_now`` directly;
    this shim is kept so the existing endpoint and any external
    automation continue to work.
    """
    try:
        from services import archive_watchdog
        return archive_watchdog.force_prune_now()
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "trigger_archive_cleanup: archive_watchdog.force_prune_now failed",
        )
        return {
            "deleted_count": 0,
            "freed_bytes": 0,
            "scanned": 0,
            "error": str(e),
        }
