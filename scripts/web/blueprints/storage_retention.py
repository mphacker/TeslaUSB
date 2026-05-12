"""Storage & Retention blueprint (Phase 3a.2 — closes part of #98).

Backs the **Settings → Storage & Retention** card in the web UI. Exposes
JSON endpoints for:

* ``POST /api/cleanup/policy``   — save the unified retention policy
  (writes to ``cleanup.*`` in ``config.yaml``).
* ``POST /api/cleanup/run_now``  — trigger an immediate retention pass
  (one-line wrapper for ``services.video_archive_service.trigger_archive_cleanup``,
  which itself wraps ``archive_watchdog.force_prune_now``).
* ``GET  /api/cleanup/status``   — return the latest retention summary
  (last-run timestamp, deleted count, freed bytes, kept-unsynced count,
  next-due timestamp, current resolved retention days, and the
  ``cleanup`` config block as it lives on disk so the UI can refresh
  without restart).

The endpoints are intentionally JSON-only — the rendered card lives in
``index.html`` (the existing Settings page) so users see retention in
the same place as their other archive-related controls.

Resolution contract for retention values: see
``services.archive_watchdog._resolve_retention_days`` for the canonical
fallback chain. Editing ``config.yaml`` directly continues to work; the
UI just reflects what's on disk so the file remains the single source
of truth.

This blueprint is **always registered** (no image gating) because
storage settings are a system-level concern that should be reachable
even when no disk image is present yet (e.g., during initial setup).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import yaml
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

storage_retention_bp = Blueprint(
    'storage_retention', __name__,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Folders the user is allowed to set per-folder overrides for. Anything
# outside this set is rejected from POST payloads to keep the config
# file from accumulating typo'd keys that the watchdog would silently
# ignore. Keep this in sync with cleanup_service.DEFAULT_POLICY_TEMPLATES
# and the legacy Tesla USB folder layout.
ALLOWED_FOLDER_NAMES = (
    'SentryClips',
    'SavedClips',
    'RecentClips',
    'EncryptedClips',
    'ArchivedClips',
)

# UI / safety bounds on the unified scalar settings. The watchdog
# accepts anything but the UI clamps to keep the system sane.
RETENTION_DAYS_MIN = 1
RETENTION_DAYS_MAX = 3650            # 10 years — a soft sanity cap
FREE_SPACE_PCT_MIN = 5
FREE_SPACE_PCT_MAX = 50
MAX_ARCHIVE_GB_MIN = 0               # 0 = no cap
MAX_ARCHIVE_GB_MAX = 10000           # 10 TB — absurdly high but bounded

# Bound the number of folder rows accepted in one POST so a malicious
# or buggy client can't bloat config.yaml with hundreds of entries.
MAX_POLICY_ROWS = len(ALLOWED_FOLDER_NAMES)


def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    """Clamp ``value`` to ``[lo, hi]``; fall back to ``default`` on parse failure.

    Bool is rejected (``isinstance(True, int)`` is True in Python — we
    do NOT want a checkbox value silently becoming 0/1 here).
    """
    if isinstance(value, bool):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on', 'checked')
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _load_config_dict() -> Dict[str, Any]:
    """Read the on-disk config.yaml. Returns an empty dict on any failure
    so callers can render an empty-state UI instead of crashing."""
    try:
        from config import CONFIG_YAML
        with open(CONFIG_YAML, 'r') as f:
            data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        logger.exception("storage_retention: failed to read config.yaml")
    return {}


def _resolve_cleanup_block(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``cleanup`` config block with defaults filled in."""
    if cfg is None:
        cfg = _load_config_dict()
    raw = cfg.get('cleanup') if isinstance(cfg.get('cleanup'), dict) else {}
    policies_raw = raw.get('policies') if isinstance(raw.get('policies'), dict) else {}
    sanitized_policies: Dict[str, Dict[str, Any]] = {}
    for name, policy in policies_raw.items():
        if name in ALLOWED_FOLDER_NAMES and isinstance(policy, dict):
            sanitized_policies[name] = {
                'enabled': _coerce_bool(policy.get('enabled'), False),
                'retention_days': _coerce_int(
                    policy.get('retention_days'),
                    default=int(raw.get('default_retention_days') or 30),
                    lo=RETENTION_DAYS_MIN,
                    hi=RETENTION_DAYS_MAX,
                ),
            }
    return {
        'default_retention_days': _coerce_int(
            raw.get('default_retention_days'),
            default=30, lo=RETENTION_DAYS_MIN, hi=RETENTION_DAYS_MAX,
        ),
        'free_space_target_pct': _coerce_int(
            raw.get('free_space_target_pct'),
            default=10, lo=FREE_SPACE_PCT_MIN, hi=FREE_SPACE_PCT_MAX,
        ),
        'max_archive_size_gb': _coerce_int(
            raw.get('max_archive_size_gb'),
            default=0, lo=MAX_ARCHIVE_GB_MIN, hi=MAX_ARCHIVE_GB_MAX,
        ),
        'short_retention_warning_days': _coerce_int(
            raw.get('short_retention_warning_days'),
            default=7, lo=1, hi=RETENTION_DAYS_MAX,
        ),
        'policies': sanitized_policies,
    }


def _watchdog_status() -> Dict[str, Any]:
    """Return the watchdog's most recent retention summary, or a synthetic
    one if the watchdog hasn't started yet."""
    try:
        from services import archive_watchdog
        return archive_watchdog.get_status() or {}
    except Exception:  # noqa: BLE001
        logger.exception("storage_retention: archive_watchdog.get_status raised")
        return {}


def _disk_free_summary() -> Dict[str, Any]:
    """Best-effort free-space snapshot for the SD card (the partition that
    holds ArchivedClips). Returns empty dict on failure — the UI hides
    the bar in that case."""
    try:
        from config import ARCHIVE_DIR
        # Walk up to a real existing path so statvfs doesn't trip on a
        # not-yet-created ArchivedClips dir.
        target = ARCHIVE_DIR
        while target and not os.path.exists(target):
            parent = os.path.dirname(target)
            if parent == target:
                break
            target = parent
        if not target or not os.path.exists(target):
            return {}
        st = os.statvfs(target)
        total_bytes = int(st.f_blocks) * int(st.f_frsize)
        free_bytes = int(st.f_bavail) * int(st.f_frsize)
        used_bytes = max(total_bytes - free_bytes, 0)
        free_pct = (
            int(round(100.0 * free_bytes / total_bytes))
            if total_bytes > 0 else 0
        )
        return {
            'path': target,
            'total_bytes': total_bytes,
            'free_bytes': free_bytes,
            'used_bytes': used_bytes,
            'free_pct': free_pct,
        }
    except Exception:  # noqa: BLE001
        logger.exception("storage_retention: disk-free probe failed")
        return {}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@storage_retention_bp.route('/api/cleanup/status', methods=['GET'])
def api_cleanup_status():
    """Return a single snapshot the UI can poll to render the card.

    Combines:
      * the on-disk ``cleanup`` config block (so direct YAML edits are
        reflected without restart);
      * the watchdog's most recent retention summary
        (``last_prune_at``, ``last_prune_deleted``, etc.);
      * the currently resolved retention days (after fallback chain);
      * an SD-card free-space snapshot.

    All fields are best-effort: a missing or crashed dependency yields
    an empty object for that subsection rather than a 500.
    """
    cfg = _load_config_dict()
    cleanup = _resolve_cleanup_block(cfg)
    status = _watchdog_status()
    retention_block = status.get('retention') if isinstance(status.get('retention'), dict) else {}
    return jsonify({
        'success': True,
        'config': cleanup,
        'resolved_retention_days': int(
            retention_block.get('retention_days')
            or cleanup['default_retention_days']
        ),
        'last_run': {
            'at': retention_block.get('last_prune_at'),
            'deleted_count': retention_block.get('last_prune_deleted'),
            'freed_bytes': retention_block.get('last_prune_freed_bytes'),
            'kept_unsynced_count': retention_block.get('last_prune_kept_unsynced'),
            'error': retention_block.get('last_prune_error'),
        },
        'next_run_at': retention_block.get('next_prune_due_at'),
        'delete_unsynced': retention_block.get('delete_unsynced'),
        'cloud_configured': retention_block.get('cloud_configured', False),
        'watchdog_running': bool(status.get('watchdog_running', False)),
        'disk': _disk_free_summary(),
    }), 200


@storage_retention_bp.route('/api/cleanup/policy', methods=['POST'])
def api_cleanup_policy():
    """Persist the unified Storage & Retention settings to ``config.yaml``.

    Accepts either JSON or form-encoded payloads. Unknown folder names
    are silently dropped (defense in depth — see ``ALLOWED_FOLDER_NAMES``).
    All scalar values are clamped to their UI-safe range; the watchdog
    re-resolves on its next pass so no service restart is needed.

    Returns the persisted ``cleanup`` block on success so the client can
    update the rendered form without a follow-up GET.
    """
    payload: Dict[str, Any] = {}
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        # Form: scalars come in flat; per-folder rows come as
        # policy_<name>_enabled / policy_<name>_days.
        payload['default_retention_days'] = request.form.get('default_retention_days')
        payload['free_space_target_pct'] = request.form.get('free_space_target_pct')
        payload['max_archive_size_gb'] = request.form.get('max_archive_size_gb')
        payload['short_retention_warning_days'] = request.form.get('short_retention_warning_days')
        policies: Dict[str, Dict[str, Any]] = {}
        for name in ALLOWED_FOLDER_NAMES:
            row_enabled = request.form.get(f'policy_{name}_enabled')
            row_days = request.form.get(f'policy_{name}_days')
            if row_enabled is None and row_days is None:
                continue
            policies[name] = {
                'enabled': row_enabled,
                'retention_days': row_days,
            }
        if policies:
            payload['policies'] = policies

    default_days = _coerce_int(
        payload.get('default_retention_days'),
        default=30, lo=RETENTION_DAYS_MIN, hi=RETENTION_DAYS_MAX,
    )
    target_pct = _coerce_int(
        payload.get('free_space_target_pct'),
        default=10, lo=FREE_SPACE_PCT_MIN, hi=FREE_SPACE_PCT_MAX,
    )
    max_gb = _coerce_int(
        payload.get('max_archive_size_gb'),
        default=0, lo=MAX_ARCHIVE_GB_MIN, hi=MAX_ARCHIVE_GB_MAX,
    )
    warn_days = _coerce_int(
        payload.get('short_retention_warning_days'),
        default=7, lo=1, hi=RETENTION_DAYS_MAX,
    )

    raw_policies = payload.get('policies') or {}
    sanitized: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_policies, dict):
        for name, policy in list(raw_policies.items())[:MAX_POLICY_ROWS]:
            if name not in ALLOWED_FOLDER_NAMES or not isinstance(policy, dict):
                continue
            sanitized[name] = {
                'enabled': _coerce_bool(policy.get('enabled'), False),
                'retention_days': _coerce_int(
                    policy.get('retention_days'),
                    default=default_days,
                    lo=RETENTION_DAYS_MIN,
                    hi=RETENTION_DAYS_MAX,
                ),
            }

    updates = {
        'cleanup.default_retention_days': default_days,
        'cleanup.free_space_target_pct': target_pct,
        'cleanup.max_archive_size_gb': max_gb,
        'cleanup.short_retention_warning_days': warn_days,
        'cleanup.policies': sanitized,
    }
    try:
        from helpers.config_updater import update_config_yaml
        update_config_yaml(updates)
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_retention: failed to persist cleanup policy")
        return jsonify({
            'success': False,
            'message': f"Failed to save retention settings: {exc}",
        }), 500

    persisted = _resolve_cleanup_block(_load_config_dict())
    return jsonify({
        'success': True,
        'config': persisted,
    }), 200


@storage_retention_bp.route('/api/cleanup/run_now', methods=['POST'])
def api_cleanup_run_now():
    """Trigger an immediate retention pass.

    One-line wrapper for ``trigger_archive_cleanup`` (which itself wraps
    ``archive_watchdog.force_prune_now``). Mirrors the same HTTP
    contract as ``POST /cloud/api/archive_cleanup`` (see Phase 3a.1
    review fix in ``cloud_archive.py``):

      * 200 on success — body includes the watchdog summary.
      * 200 on ``status='already_running'`` — normal control flow when
        a periodic prune is in flight; the UI shows "Cleanup already
        in progress" instead of an error toast.
      * 500 on watchdog error — body includes the error message and
        the structured summary for debugging.

    Provided as a separate route from ``/cloud/api/archive_cleanup`` so
    the UI can use a semantically-named endpoint (``/api/cleanup/...``)
    and we have a stable hook for future enhancements (e.g. async
    long-running cleanup with progress events).
    """
    try:
        from services.video_archive_service import trigger_archive_cleanup
        result = trigger_archive_cleanup()
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_retention: trigger_archive_cleanup raised")
        return jsonify({
            'success': False,
            'message': f"Cleanup failed: {exc}",
        }), 500

    if isinstance(result, dict) and result.get('error'):
        return jsonify({
            'success': False,
            'message': str(result.get('error')),
            'result': result,
        }), 500

    return jsonify({
        'success': True,
        'result': result if isinstance(result, dict) else {},
    }), 200
