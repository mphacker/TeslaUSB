#!/usr/bin/env python3
"""WiFi connect handler — drive the post-arrival workflow.

Called by the NetworkManager dispatcher (``99-teslausb-cloud-refresh``)
whenever ``wlan0`` comes up. This is the "car arrived home" trigger:

1. Refresh the read-only mount so the local file watcher sees Tesla's
   latest writes.
2. Trigger a RecentClips → SD-card archive run via the long-lived
   ``gadget_web`` HTTP API. (Daemon threads started in this short-lived
   dispatcher process die when it exits, so we route the work through
   the always-on web service.)
3. Wait — bounded — for the archive run and the indexing-queue drain,
   so cloud sync uploads include the freshly archived/indexed clips.
4. Trigger cloud sync (also via the gadget_web HTTP API, for the same
   daemon-thread-lifetime reason).

Every "wait" step has a hard cap so a hung backend can never make this
script run forever and pile up dispatcher invocations.
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

WEB_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, WEB_DIR)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [wifi-handler] %(message)s",
)
logger = logging.getLogger(__name__)


# Hard caps on the bounded waits. The dispatcher fires on every WiFi
# up event; if the script ran for an unbounded time we'd accumulate
# overlapping invocations.
_ARCHIVE_TIMEOUT_SECONDS = 5 * 60   # 5 min — RecentClips is small
_INDEX_TIMEOUT_SECONDS = 120        # 2 min — front-cam priority enqueues first
_LIVE_EVENT_TIMEOUT_SECONDS = 10 * 60  # 10 min — Sentry events are ~6 small clips
_HTTP_TIMEOUT_SECONDS = 10
_WEB_BASE = "http://localhost"


def _http_get_json(path: str, timeout: float = _HTTP_TIMEOUT_SECONDS):
    req = urllib.request.Request(_WEB_BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def _http_post_json(path: str, payload=None,
                    timeout: float = _HTTP_TIMEOUT_SECONDS):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        _WEB_BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def _trigger_recent_archive() -> bool:
    """Fire the recent-clips archive endpoint. Non-blocking on the server."""
    try:
        result = _http_post_json("/api/recent_archive/trigger")
        logger.info(
            "Recent-clips archive trigger: started=%s message=%s",
            result.get("started"), result.get("message"),
        )
        return bool(result.get("started"))
    except Exception as e:  # noqa: BLE001
        logger.warning("Recent-clips archive trigger failed: %s", e)
        return False


def _wait_for_recent_archive(deadline_seconds: float) -> None:
    """Poll the archive status until idle or the deadline elapses."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            status = _http_get_json("/api/recent_archive/status")
            if not status.get("running"):
                logger.info("Recent-clips archive complete.")
                return
        except Exception as e:  # noqa: BLE001
            logger.warning("Archive status poll failed (will retry): %s", e)
        time.sleep(5)
    logger.info(
        "Recent-clips archive still running after %.0fs — moving on.",
        deadline_seconds,
    )


def _wait_for_index_drain(deadline_seconds: float) -> None:
    """Poll the indexing-queue status until empty or the deadline elapses.

    "Drained enough to start cloud sync" means:
      * No file is being actively parsed RIGHT NOW
        (``active_file is None``), AND
      * No queue row is ready to be picked up RIGHT NOW
        (``next_ready_at`` is in the future, or there are no
        ready rows at all).

    We deliberately do NOT wait for deferred rows (TOO_NEW or the
    archive flow's 120 s safety net). Cloud sync runs on every
    dispatcher fire, so any latecomer will be uploaded the next time
    the car arrives home.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            status = _http_get_json("/api/index/status")
            active = status.get("active_file")
            next_ready = status.get("next_ready_at")
            now_wall = time.time()
            ready_now = (next_ready is not None and next_ready <= now_wall)
            if not active and not ready_now:
                logger.info("Indexing queue idle (next_ready_at=%s).",
                            next_ready)
                return
        except Exception as e:  # noqa: BLE001
            logger.warning("Index status poll failed (will retry): %s", e)
        time.sleep(3)
    logger.info(
        "Indexing queue still has work after %.0fs — moving on.",
        deadline_seconds,
    )


def _trigger_cloud_sync() -> None:
    """Trigger cloud sync via the gadget_web API (long-lived process)."""
    try:
        from config import (
            CLOUD_ARCHIVE_ENABLED, CLOUD_PROVIDER_CREDS_PATH,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read cloud archive config: %s", e)
        return

    if not CLOUD_ARCHIVE_ENABLED:
        logger.info("Cloud archive disabled — skipping sync.")
        return
    if not os.path.isfile(CLOUD_PROVIDER_CREDS_PATH):
        logger.info("Cloud provider credentials missing — skipping sync.")
        return

    try:
        result = _http_post_json("/cloud/api/sync_now")
        logger.info("Cloud sync triggered: %s", str(result)[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("Cloud sync trigger failed: %s", e)


def _wake_live_event_sync() -> None:
    """Poke the Live Event Sync worker so it drains BEFORE cloud sync starts.

    LES is the priority subsystem when both want WiFi. The endpoint is a
    no-op when LES is disabled.
    """
    try:
        result = _http_post_json("/api/live_events/wake")
        logger.info("LES wake: enabled=%s", result.get("enabled"))
    except Exception as e:  # noqa: BLE001
        logger.debug("LES wake skipped: %s", e)


def _wait_for_live_event_drain(deadline_seconds: float) -> None:
    """Bounded poll until LES has no *ready* work (or deadline elapses).

    Live events take strict priority over cloud_archive: we wait for
    them to drain before triggering cloud sync. Cap the wait so a long
    cellular outage doesn't block cloud_archive forever.

    "Drained" here means LES has nothing it could be doing right now —
    not necessarily an empty queue. We use the server-side
    ``has_ready_work`` flag (which already drives cloud_archive's
    yielding logic) as the source of truth: a row that is ``pending``
    but in backoff (``next_retry_at`` in the future), over the retry
    cap, or paused for the daily data cap should NOT block
    cloud_archive for the full 10-minute deadline.

    The queue is treated as drained when:

      * ``has_ready_work`` is False, AND
      * nothing is currently in flight (``uploading == 0``), AND
      * the worker is not actively processing a row (``running``
        is falsy).

    Legacy fallback: if the server response predates this field
    (e.g. user hasn't restarted ``gadget_web`` after upgrading), we
    keep the old ``pending + uploading == 0`` behaviour so an
    out-of-date server doesn't accidentally let cloud_archive race
    LES.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            status = _http_get_json("/api/live_events/status")
            if not status.get("enabled"):
                return
            counts = status.get("queue_counts") or {}
            uploading = counts.get("uploading", 0) or 0
            running = bool(status.get("running"))
            if 'has_ready_work' in status:
                if (not status.get("has_ready_work", False)
                        and uploading == 0
                        and not running):
                    logger.info("LES has no ready work.")
                    return
            else:
                # Out-of-date server (no has_ready_work field yet).
                pending = (counts.get("pending", 0) or 0) + uploading
                if pending == 0 and not running:
                    logger.info("LES queue idle (legacy semantics).")
                    return
        except Exception as e:  # noqa: BLE001
            logger.warning("LES status poll failed (will retry): %s", e)
        time.sleep(3)
    logger.info(
        "LES queue still has work after %.0fs — moving on.",
        deadline_seconds,
    )


def main() -> int:
    from services.video_service import get_teslacam_path

    teslacam = get_teslacam_path()
    if not teslacam:
        logger.info("TeslaCam path not available — skipping")
        return 0

    # Step 1: Refresh the RO mount so the local file watcher (and the
    # boot catch-up scan that the worker already runs in idle ticks)
    # see Tesla's latest files.
    try:
        from services.mapping_service import _refresh_ro_mount
        _refresh_ro_mount(teslacam)
        logger.info("RO mount refreshed")
    except Exception as e:  # noqa: BLE001
        logger.warning("Mount refresh failed (non-fatal): %s", e)

    # Step 2: Wake the Live Event Sync worker IMMEDIATELY so any
    # queued sentry/save events start uploading right away. LES gets
    # priority over cloud_archive when both want WiFi.
    _wake_live_event_sync()

    # Step 3: Trigger RecentClips → SD-card archive in the long-lived
    # gadget_web process. (Older versions of this script started the
    # archive thread in the dispatcher process directly, which meant
    # the daemon thread died as soon as the script exited.)
    started = _trigger_recent_archive()
    if started:
        _wait_for_recent_archive(_ARCHIVE_TIMEOUT_SECONDS)

    # Step 4: Bounded wait for the indexing queue to drain. The
    # archive run above pre-enqueues each newly archived clip (with
    # a short defer to avoid racing the inline parse), so by the
    # time we reach this point the queue should be very small. Cap
    # at _INDEX_TIMEOUT_SECONDS so a slow parse doesn't block cloud
    # sync indefinitely — the cloud sync will pick up any latecomers
    # on the next dispatcher fire.
    _wait_for_index_drain(_INDEX_TIMEOUT_SECONDS)

    # Step 5: Bounded wait for LES to drain so cloud sync doesn't
    # contend with it for the upload bandwidth or the task_coordinator.
    _wait_for_live_event_drain(_LIVE_EVENT_TIMEOUT_SECONDS)

    # Step 6: Trigger cloud sync. Same daemon-thread-lifetime reason
    # — done via the long-lived gadget_web process.
    _trigger_cloud_sync()

    return 0


if __name__ == "__main__":
    sys.exit(main())
