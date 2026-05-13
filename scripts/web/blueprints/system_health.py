"""System Health endpoint — single-poll snapshot for the Settings card.

Phase 4.2 (issue #101) collapses every background-subsystem status feed
into one cheap JSON payload so the at-a-glance card on Settings (and the
nav-bar status dot in Phase 4.8) can render with a single HTTP request.

Design rules
------------
* **Cheap.** No subprocesses on the hot path. WiFi/AP probes spawn
  ``nmcli``/``sudo bash`` and take ~50–200 ms each — they get a 30 s
  TTL cache so the 5 s poll loop the dot will use cannot pin a CPU
  core. Every other subsystem already has an in-memory snapshot
  helper; we just call those.
* **Fault-tolerant.** Any subsystem that raises is reported as
  ``severity: "unknown"`` with a one-line error; the page always
  renders. One bad SQLite DB cannot make the rest of the dashboard
  500.
* **Stable shape.** Every subsystem block has ``severity`` (``ok`` /
  ``warn`` / ``error`` / ``unknown``) and ``message`` (≤ 80 chars,
  user-friendly). The dot can colour itself purely from ``severity``;
  the card can render the message verbatim.
* **No identifier disclosure.** ``message`` strings are short
  user-facing labels — they MUST NOT contain absolute paths, rclone
  bucket names, or other identifiers an LAN/AP guest doesn't need.
  This mirrors the redaction contract from the Failed Jobs page.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from typing import Any, Callable, Dict, Tuple

from flask import Blueprint, jsonify

from config import (
    ARCHIVE_QUEUE_ENABLED,
    CLOUD_ARCHIVE_ENABLED,
    GADGET_DIR,
    LIVE_EVENT_SYNC_ENABLED,
    MAPPING_ENABLED,
)

logger = logging.getLogger(__name__)

system_health_bp = Blueprint('system_health', __name__)


# ---------------------------------------------------------------------------
# Severity vocabulary
# ---------------------------------------------------------------------------

SEV_OK = 'ok'
SEV_WARN = 'warn'
SEV_ERROR = 'error'
SEV_UNKNOWN = 'unknown'

# Ranking used for the rolled-up ``overall`` block. A subsystem in
# ``unknown`` is not as bad as an ``error`` — the worker may simply be
# disabled — but it should outrank a healthy ``ok`` so the dot still
# turns amber when something is silently broken.
_SEV_RANK = {SEV_OK: 0, SEV_UNKNOWN: 1, SEV_WARN: 2, SEV_ERROR: 3}


# ---------------------------------------------------------------------------
# 30 s TTL cache for shell-out probes (WiFi / AP)
# ---------------------------------------------------------------------------

_SHELL_PROBE_TTL_SECONDS = 30.0
_probe_cache: Dict[str, Tuple[float, Any]] = {}
_probe_lock = threading.Lock()
# Per-name in-flight locks: a concurrent cold-cache burst on the same
# probe name (e.g. two visibility-change events landing simultaneously)
# would otherwise double-spawn ``nmcli``/``sudo bash`` because we drop
# the global lock around ``fn()``. The per-name lock serialises probes
# of the same name without blocking unrelated probes.
_probe_inflight: Dict[str, threading.Lock] = {}


def _cached_probe(name: str, fn: Callable[[], Any]) -> Any:
    """Return ``fn()`` cached for :data:`_SHELL_PROBE_TTL_SECONDS`.

    Also recovers from probe failure: on exception we cache the error
    string for the same TTL so a misbehaving subprocess can't flood
    the page with retries.

    Concurrency: per-probe-name in-flight lock guarantees only one
    ``fn()`` invocation per name regardless of caller count, so a cold
    cache burst cannot stack subprocesses.
    """
    now = time.time()
    with _probe_lock:
        cached = _probe_cache.get(name)
        if cached and now - cached[0] < _SHELL_PROBE_TTL_SECONDS:
            return cached[1]
        inflight = _probe_inflight.setdefault(name, threading.Lock())

    with inflight:
        # Re-check cache after acquiring per-name lock — another caller
        # may have just populated it while we waited.
        now = time.time()
        with _probe_lock:
            cached = _probe_cache.get(name)
            if cached and now - cached[0] < _SHELL_PROBE_TTL_SECONDS:
                return cached[1]
        try:
            value = fn()
        except Exception as e:  # noqa: BLE001
            logger.warning("system_health probe %s failed: %s", name, e)
            value = {'_error': str(e)[:120]}
        with _probe_lock:
            _probe_cache[name] = (time.time(), value)
        return value


# ---------------------------------------------------------------------------
# Per-subsystem snapshots
# ---------------------------------------------------------------------------

def _indexer_block() -> Dict[str, Any]:
    """Indexer worker liveness + queue depth."""
    if not MAPPING_ENABLED:
        return {
            'severity': SEV_UNKNOWN,
            'message': 'Indexing disabled in config',
            'enabled': False,
            'queue_depth': 0,
            'worker_running': False,
        }
    try:
        from services import indexing_worker  # type: ignore
        snap = indexing_worker.get_worker_status() or {}
    except Exception as e:  # noqa: BLE001
        return {
            'severity': SEV_UNKNOWN,
            'message': 'Status fetch failed',
            'enabled': True,
            'queue_depth': 0,
            'worker_running': False,
            '_error': str(e)[:120],
        }

    running = bool(snap.get('worker_running'))
    queue_depth = int(snap.get('queue_depth') or 0)
    dead = int(snap.get('dead_letter_count') or 0)

    if not running:
        sev = SEV_ERROR
        msg = 'Worker not running'
    elif dead > 0:
        sev = SEV_WARN
        msg = f'{dead} dead-letter row{"s" if dead != 1 else ""}'
    elif queue_depth > 100:
        sev = SEV_WARN
        msg = f'{queue_depth} queued (catch-up)'
    else:
        sev = SEV_OK
        msg = (f'{queue_depth} queued'
               if queue_depth else 'Idle, queue empty')

    return {
        'severity': sev,
        'message': msg,
        'enabled': True,
        'worker_running': running,
        'queue_depth': queue_depth,
        'dead_letter_count': dead,
        'active': bool(snap.get('active_file')),
    }


def _archive_block() -> Dict[str, Any]:
    """Archive watchdog + worker status."""
    if not ARCHIVE_QUEUE_ENABLED:
        return {
            'severity': SEV_UNKNOWN,
            'message': 'Archive queue disabled',
            'enabled': False,
            'paused': False,
            'queue_depth': 0,
            'lost_24h': 0,
        }
    try:
        from services import archive_queue, archive_watchdog, archive_worker
        watchdog = archive_watchdog.get_status() or {}
        worker = archive_worker.get_status() or {}
        counts = archive_queue.get_queue_status() or {}
        # Phase 4.3: count files Tesla rotated out before we copied them
        # in the last 24 h. Cheap indexed COUNT(*); safe on every poll.
        try:
            lost_24h = int(archive_queue.count_source_gone_recent(24) or 0)
        except Exception:  # noqa: BLE001 — never let a counter kill the page
            lost_24h = 0
    except Exception as e:  # noqa: BLE001
        return {
            'severity': SEV_UNKNOWN,
            'message': 'Status fetch failed',
            'enabled': True,
            'paused': False,
            'queue_depth': 0,
            'lost_24h': 0,
            '_error': str(e)[:120],
        }

    paused = bool(worker.get('paused'))
    running = bool(worker.get('worker_running'))
    pending = int(counts.get('pending', 0))
    dead = int(counts.get('dead_letter', 0))

    # Watchdog severity is the single source of truth for "should the
    # operator be alarmed". We translate its 4-level ladder into the
    # health card's 4-level vocabulary 1:1.
    wd_sev = (watchdog.get('severity') or 'ok').lower()
    if wd_sev not in (SEV_OK, SEV_WARN, SEV_ERROR):
        wd_sev = SEV_UNKNOWN

    if not running:
        sev = SEV_ERROR
        msg = 'Worker not running'
    elif wd_sev == SEV_ERROR:
        sev = SEV_ERROR
        msg = (watchdog.get('message') or 'Watchdog error')[:80]
    elif lost_24h > 0:
        # Lost-files dominates dead-letters because lost footage is
        # unrecoverable, whereas a dead-letter row still has the source
        # data on the SD card and can be retried.
        sev = SEV_WARN
        msg = (f'{lost_24h} clip{"s" if lost_24h != 1 else ""} '
               'lost in last 24h')
    elif dead > 0:
        sev = SEV_WARN
        msg = f'{dead} dead-letter row{"s" if dead != 1 else ""}'
    elif paused:
        sev = SEV_WARN
        msg = 'Paused (load or disk)'
    elif wd_sev == SEV_WARN:
        sev = SEV_WARN
        msg = (watchdog.get('message') or 'Watchdog warn')[:80]
    elif pending > 200:
        sev = SEV_WARN
        msg = f'{pending} pending (catch-up)'
    else:
        sev = SEV_OK
        msg = (f'{pending} pending'
               if pending else 'Idle, queue empty')

    return {
        'severity': sev,
        'message': msg,
        'enabled': True,
        'worker_running': running,
        'paused': paused,
        'queue_depth': pending,
        'dead_letter_count': dead,
        'lost_24h': lost_24h,
    }


def _cloud_block() -> Dict[str, Any]:
    """Cloud archive worker status + queue counts."""
    if not CLOUD_ARCHIVE_ENABLED:
        return {
            'severity': SEV_UNKNOWN,
            'message': 'Cloud archive disabled',
            'enabled': False,
            'queue_depth': 0,
        }
    try:
        from services.cloud_archive_service import (
            count_dead_letters, get_sync_status,
        )
        sync = get_sync_status() or {}
        dead = int(count_dead_letters() or 0)
    except Exception as e:  # noqa: BLE001
        return {
            'severity': SEV_UNKNOWN,
            'message': 'Status fetch failed',
            'enabled': True,
            'queue_depth': 0,
            '_error': str(e)[:120],
        }

    running = bool(sync.get('running'))
    pending = int(sync.get('files_total', 0)) - int(sync.get('files_done', 0))
    if pending < 0:
        pending = 0

    if dead > 0:
        sev = SEV_WARN
        msg = f'{dead} dead-letter row{"s" if dead != 1 else ""}'
    elif running:
        sev = SEV_OK
        msg = (f'Uploading ({pending} pending)'
               if pending else 'Uploading')
    elif pending > 0:
        sev = SEV_OK
        msg = f'{pending} queued for next WiFi'
    else:
        sev = SEV_OK
        msg = 'Idle, queue empty'

    return {
        'severity': sev,
        'message': msg,
        'enabled': True,
        'running': running,
        'queue_depth': pending,
        'dead_letter_count': dead,
        'last_sync_at': sync.get('last_completed_at'),
    }


def _les_block() -> Dict[str, Any]:
    """Live Event Sync status."""
    if not LIVE_EVENT_SYNC_ENABLED:
        return {
            'severity': SEV_UNKNOWN,
            'message': 'LES disabled in config',
            'enabled': False,
            'queue_depth': 0,
        }
    try:
        from services.live_event_sync_service import (
            count_failed, get_status,
        )
        snap = get_status() or {}
        failed = int(count_failed() or 0)
    except Exception as e:  # noqa: BLE001
        return {
            'severity': SEV_UNKNOWN,
            'message': 'Status fetch failed',
            'enabled': True,
            'queue_depth': 0,
            '_error': str(e)[:120],
        }

    counts = snap.get('queue_counts') or {}
    pending = int(counts.get('pending', 0)) + int(counts.get('uploading', 0))
    running = bool(snap.get('worker_running'))

    if failed > 0:
        sev = SEV_WARN
        msg = f'{failed} failed event{"s" if failed != 1 else ""}'
    elif not running:
        sev = SEV_WARN
        msg = 'Worker idle'
    elif pending > 0:
        sev = SEV_OK
        msg = f'{pending} pending'
    else:
        sev = SEV_OK
        msg = 'Idle, queue empty'

    return {
        'severity': sev,
        'message': msg,
        'enabled': True,
        'worker_running': running,
        'queue_depth': pending,
        'failed_count': failed,
        'last_uploaded_at': snap.get('last_uploaded_at'),
    }


def _disk_block() -> Dict[str, Any]:
    """SD card free space (the home-directory filesystem)."""
    target = GADGET_DIR or '/home/pi'
    try:
        usage = shutil.disk_usage(target)
    except OSError as e:
        return {
            'severity': SEV_UNKNOWN,
            'message': 'Disk usage probe failed',
            '_error': str(e)[:120],
        }

    total_gb = usage.total / (1024 ** 3)
    free_gb = usage.free / (1024 ** 3)
    used_pct = (usage.used / usage.total) * 100 if usage.total else 0.0

    if used_pct >= 95:
        sev = SEV_ERROR
        msg = f'Critical: {used_pct:.1f}% full'
    elif used_pct >= 85:
        sev = SEV_WARN
        msg = f'{used_pct:.1f}% full'
    else:
        sev = SEV_OK
        msg = f'{free_gb:.1f} GB free'

    return {
        'severity': sev,
        'message': msg,
        'used_pct': round(used_pct, 1),
        'free_gb': round(free_gb, 2),
        'total_gb': round(total_gb, 2),
    }


def _wifi_block() -> Dict[str, Any]:
    """STA WiFi state + AP active flag (cached for 30 s)."""
    sta = _cached_probe('wifi_sta', _probe_wifi_sta)
    ap = _cached_probe('wifi_ap', _probe_wifi_ap)

    if isinstance(sta, dict) and sta.get('_error'):
        return {
            'severity': SEV_UNKNOWN,
            'message': 'WiFi probe failed',
            '_error': sta['_error'],
        }

    connected = bool(sta.get('connected'))
    ssid = sta.get('current_ssid') or 'Unknown'
    signal_raw = sta.get('signal')
    try:
        signal_pct = int(signal_raw) if signal_raw not in (None, '', 'Unknown') else None
    except (TypeError, ValueError):
        signal_pct = None

    ap_active = bool((ap or {}).get('ap_active'))

    if connected:
        if signal_pct is not None and signal_pct < 30:
            sev = SEV_WARN
            msg = f'{ssid} (weak: {signal_pct}%)'
        else:
            sev = SEV_OK
            sig_text = f' {signal_pct}%' if signal_pct is not None else ''
            msg = f'{ssid}{sig_text}'
    elif ap_active:
        sev = SEV_WARN
        msg = 'STA offline — AP active'
    else:
        sev = SEV_ERROR
        msg = 'No WiFi'

    return {
        'severity': sev,
        'message': msg,
        'connected': connected,
        'ssid': ssid,
        'signal': signal_pct,
        'ap_active': ap_active,
    }


def _probe_wifi_sta() -> Dict[str, Any]:
    from services.wifi_service import get_current_wifi_connection
    return get_current_wifi_connection() or {}


def _probe_wifi_ap() -> Dict[str, Any]:
    from services.ap_service import ap_status
    return ap_status() or {}


# ---------------------------------------------------------------------------
# Aggregator + route
# ---------------------------------------------------------------------------

_BLOCKS: Tuple[Tuple[str, Callable[[], Dict[str, Any]]], ...] = (
    ('indexer', _indexer_block),
    ('archive', _archive_block),
    ('cloud', _cloud_block),
    ('live_event_sync', _les_block),
    ('disk', _disk_block),
    ('wifi', _wifi_block),
)


def _build_health() -> Dict[str, Any]:
    """Compose the full payload, isolating per-subsystem crashes."""
    payload: Dict[str, Any] = {}
    worst = SEV_OK
    worst_msg = ''
    worst_subsystem = None

    for name, fn in _BLOCKS:
        try:
            block = fn()
        except Exception as e:  # noqa: BLE001 — never let one block 500 the page
            logger.exception("system_health: %s block crashed", name)
            block = {
                'severity': SEV_UNKNOWN,
                'message': 'Block error',
                '_error': str(e)[:120],
            }
        payload[name] = block

        sev = block.get('severity', SEV_UNKNOWN)
        if _SEV_RANK.get(sev, 0) > _SEV_RANK.get(worst, 0):
            worst = sev
            worst_msg = block.get('message', '')
            worst_subsystem = name

    payload['overall'] = {
        'severity': worst,
        'message': (
            f'{worst_subsystem}: {worst_msg}'
            if worst != SEV_OK and worst_subsystem else 'All systems normal'
        ),
        'subsystem': worst_subsystem,
    }
    payload['generated_at'] = int(time.time())
    return payload


@system_health_bp.route('/api/system/health', methods=['GET'])
def api_system_health():
    """Return one JSON snapshot of every background subsystem.

    Used by the Settings system-health card and (Phase 4.8) the
    nav-bar status dot. Both poll on a fixed interval, so this
    endpoint MUST stay sub-100 ms in the cached path.
    """
    return jsonify(_build_health())
