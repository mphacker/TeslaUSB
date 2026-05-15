"""Settings → Advanced sub-page (Phase 5.9, issue #102).

Surfaces the handful of background-worker tunables that previously lived
as hardcoded constants in the services. Casual users will never see this
page — it is reachable only via a low-key "Advanced settings →" link at
the bottom of the main Settings page (NOT a top-level nav item).

All writes go through ``helpers.config_updater.update_config_yaml`` (the
same atomic temp+fsync+rename writer the other settings forms use). A
restart of ``gadget_web.service`` is still required for the new values
to take effect — that is communicated by the flash message.

Each tunable has:

* ``key``               — dotted path in ``config.yaml``
* ``form_name``         — POST field name (must be a valid HTML name)
* ``label``             — short friendly label
* ``description``       — one-line help text shown under the input
* ``current``           — current value (read fresh from config.py)
* ``default``           — factory default for the "Reset to default" button
* ``unit``              — suffix shown in the UI (e.g. "seconds")
* ``min``, ``max``      — HTML5 range bounds; also enforced server-side
* ``step``              — HTML5 step value
* ``cast``              — Python callable to coerce the form value before save

Validation: server-side ``cast`` raises ``ValueError`` on out-of-range
inputs and the route flashes the error and stays on the page; the form
will not be persisted partial-success even when multiple fields are
posted (we accumulate updates and apply them in one ``update_config_yaml``
call so a bad field aborts the whole save).
"""
import logging
from typing import Any, Dict, List

from flask import Blueprint, render_template, request, redirect, url_for, flash

from utils import get_base_context

settings_advanced_bp = Blueprint(
    'settings_advanced',
    __name__,
    url_prefix='/settings/advanced',
)

logger = logging.getLogger(__name__)


def _bounded_float(min_val: float, max_val: float):
    """Build a cast callable that returns a float in ``[min_val, max_val]``."""
    def _cast(raw: str) -> float:
        v = float(raw)
        if v < min_val or v > max_val:
            raise ValueError(
                f"value {v} out of range [{min_val}, {max_val}]"
            )
        return v
    return _cast


def _bounded_int(min_val: int, max_val: int):
    """Build a cast callable that returns an int in ``[min_val, max_val]``."""
    def _cast(raw: str) -> int:
        v = int(float(raw))  # accept "30.0" too
        if v < min_val or v > max_val:
            raise ValueError(
                f"value {v} out of range [{min_val}, {max_val}]"
            )
        return v
    return _cast


def _build_tunables() -> List[Dict[str, Any]]:
    """Return the live list of tunables with current values from config.

    Imports are local so the test suite can monkeypatch ``config`` after
    blueprint import without re-importing this module.
    """
    import config

    return [
        {
            'key': 'archive_queue.inter_file_sleep_seconds',
            'form_name': 'archive_inter_file_sleep_seconds',
            'label': 'Archive: inter-file sleep',
            'description': (
                'Pause between successful archive copies. Higher values '
                'reduce SDIO bus contention with WiFi but slow catch-up.'
            ),
            'current': float(config.ARCHIVE_QUEUE_INTER_FILE_SLEEP_SECONDS),
            'default': 1.0,
            'unit': 'seconds',
            'min': 0.0, 'max': 30.0, 'step': 0.1,
            'cast': _bounded_float(0.0, 30.0),
        },
        {
            'key': 'archive_queue.load_pause_threshold',
            'form_name': 'archive_load_pause_threshold',
            'label': 'Archive: load-pause threshold',
            'description': (
                '1-min loadavg above which the archive worker pauses. '
                'Default 3.5 keeps the Pi Zero 2 W from starving the '
                'watchdog daemon under SDIO load.'
            ),
            'current': float(config.ARCHIVE_QUEUE_LOAD_PAUSE_THRESHOLD),
            'default': 3.5,
            'unit': 'load',
            'min': 0.5, 'max': 10.0, 'step': 0.1,
            'cast': _bounded_float(0.5, 10.0),
        },
        {
            'key': 'archive_queue.load_pause_seconds',
            'form_name': 'archive_load_pause_seconds',
            'label': 'Archive: load-pause duration',
            'description': (
                'How long the archive worker sleeps when load is above '
                'the threshold above.'
            ),
            'current': float(config.ARCHIVE_QUEUE_LOAD_PAUSE_SECONDS),
            'default': 30.0,
            'unit': 'seconds',
            'min': 1.0, 'max': 300.0, 'step': 1.0,
            'cast': _bounded_float(1.0, 300.0),
        },
        {
            'key': 'archive_queue.chunk_pause_seconds',
            'form_name': 'archive_chunk_pause_seconds',
            'label': 'Archive: per-chunk pause',
            'description': (
                'Mid-copy sleep when load is above the threshold. '
                'Issue #104 mitigation A — keeps a single 50 MB clip '
                'from holding the SDIO bus for several minutes.'
            ),
            'current': float(config.ARCHIVE_QUEUE_CHUNK_PAUSE_SECONDS),
            'default': 0.25,
            'unit': 'seconds',
            'min': 0.0, 'max': 5.0, 'step': 0.05,
            'cast': _bounded_float(0.0, 5.0),
        },
        {
            'key': 'archive_queue.per_file_time_budget_seconds',
            'form_name': 'archive_per_file_time_budget_seconds',
            'label': 'Archive: per-file time budget',
            'description': (
                'Abort + requeue any single copy taking longer than '
                'this. Issue #104 mitigation B — bounded blast radius '
                'for a pathologically slow clip.'
            ),
            'current': float(config.ARCHIVE_QUEUE_PER_FILE_TIME_BUDGET_SECONDS),
            'default': 60.0,
            'unit': 'seconds',
            'min': 10.0, 'max': 600.0, 'step': 5.0,
            'cast': _bounded_float(10.0, 600.0),
        },
        {
            'key': 'archive_queue.stable_write_age_seconds',
            'form_name': 'archive_stable_write_age_seconds',
            'label': 'Archive: stable-write age',
            'description': (
                'Re-queue clips whose mtime is fresher than this. '
                'Avoids copying a file Tesla is still writing.'
            ),
            'current': float(config.ARCHIVE_QUEUE_STABLE_WRITE_AGE_SECONDS),
            'default': 5.0,
            'unit': 'seconds',
            'min': 0.0, 'max': 60.0, 'step': 0.5,
            'cast': _bounded_float(0.0, 60.0),
        },
        {
            'key': 'archive_queue.recent_clips_stable_write_age_seconds',
            'form_name': 'archive_recent_clips_stable_write_age_seconds',
            'label': 'Archive: RecentClips stable-write age',
            'description': (
                'Higher stable-write threshold for RecentClips. Tesla '
                'writes the moov atom at the end of each ~60s segment, '
                'so RecentClips need extra settling time before copy '
                '(see archive_worker._CopyMoovIncomplete).'
            ),
            'current': float(
                config.ARCHIVE_QUEUE_RECENT_CLIPS_STABLE_WRITE_AGE_SECONDS
            ),
            'default': 90.0,
            'unit': 'seconds',
            'min': 30.0, 'max': 300.0, 'step': 5.0,
            'cast': _bounded_float(30.0, 300.0),
        },
        {
            'key': 'archive_queue.peek_give_up_age_seconds',
            'form_name': 'archive_peek_give_up_age_seconds',
            'label': 'Archive: peek give-up age',
            'description': (
                'When the SEI peek raises a parser error AND the '
                'file is older than this, treat it as stationary '
                '(skip) instead of falling through to copy. Prevents '
                'the worker from cycling on files Tesla wrote via the '
                'gadget block layer where the Pi VFS page cache holds '
                'a stale view.'
            ),
            'current': float(config.ARCHIVE_QUEUE_PEEK_GIVE_UP_AGE_SECONDS),
            'default': 300.0,
            'unit': 'seconds',
            'min': 60.0, 'max': 3600.0, 'step': 30.0,
            'cast': _bounded_float(60.0, 3600.0),
        },
        {
            'key': 'archive_queue.stale_claim_max_age_seconds',
            'form_name': 'archive_stale_claim_max_age_seconds',
            'label': 'Archive: stale-claim max age',
            'description': (
                'Reset claimed rows older than this back to pending '
                'on worker startup. Recovers from crashed/OOMed claims.'
            ),
            'current': float(config.ARCHIVE_QUEUE_STALE_CLAIM_MAX_AGE_SECONDS),
            'default': 600.0,
            'unit': 'seconds',
            'min': 60.0, 'max': 3600.0, 'step': 30.0,
            'cast': _bounded_float(60.0, 3600.0),
        },
        {
            'key': 'mapping.index_too_new_seconds',
            'form_name': 'mapping_index_too_new_seconds',
            'label': 'Indexer: too-new threshold',
            'description': (
                'Skip indexing for clips whose mtime is fresher than '
                'this. Tesla writes the moov atom at the end of each '
                'clip, so very-fresh files may be truncated.'
            ),
            'current': float(config.MAPPING_INDEX_TOO_NEW_SECONDS),
            'default': 120.0,
            'unit': 'seconds',
            'min': 30.0, 'max': 600.0, 'step': 5.0,
            'cast': _bounded_float(30.0, 600.0),
        },
        {
            'key': 'mapping.event_detection.speed_limit_mps',
            'form_name': 'mapping_speed_limit_mps',
            'label': 'Trip-merge speed limit (m/s)',
            'description': (
                'Speed alert + trip-merge threshold in m/s '
                '(35.76 ≈ 80 mph). Set 0 to disable speed events.'
            ),
            'current': float(
                config.MAPPING_EVENT_THRESHOLDS.get('speed_limit_mps', 35.76)
            ),
            'default': 35.76,
            'unit': 'm/s',
            'min': 0.0, 'max': 100.0, 'step': 0.01,
            'cast': _bounded_float(0.0, 100.0),
        },
        {
            'key': 'archive_queue.watchdog_check_interval_seconds',
            'form_name': 'archive_watchdog_check_interval_seconds',
            'label': 'Retention scan interval',
            'description': (
                'Cadence of the archive-health watchdog tick. The '
                'worker that prunes old clips when free space drops.'
            ),
            'current': float(config.ARCHIVE_QUEUE_WATCHDOG_CHECK_INTERVAL_SECONDS),
            'default': 60.0,
            'unit': 'seconds',
            'min': 10.0, 'max': 3600.0, 'step': 10.0,
            'cast': _bounded_float(10.0, 3600.0),
        },
        {
            'key': 'archive_queue.copy_chunk_bytes',
            'form_name': 'archive_copy_chunk_bytes',
            'label': 'Archive: copy chunk size',
            'description': (
                'Bytes per write in the temp+fsync+rename copy loop. '
                'Smaller = lower per-chunk pause cost but more syscalls.'
            ),
            'current': int(config.ARCHIVE_QUEUE_COPY_CHUNK_BYTES),
            'default': 1048576,
            'unit': 'bytes',
            'min': 65536, 'max': 16777216, 'step': 65536,
            'cast': _bounded_int(65536, 16777216),
        },
    ]


@settings_advanced_bp.route('/', methods=['GET'])
def index():
    """Render the Advanced sub-page with current tunable values."""
    ctx = get_base_context()
    return render_template(
        'settings_advanced.html',
        page='settings',
        tunables=_build_tunables(),
        **ctx,
    )


@settings_advanced_bp.route('/save', methods=['POST'])
def save():
    """Persist changed tunables to ``config.yaml`` atomically.

    Strategy: walk every tunable; if its form field is present AND its
    coerced value differs from current, stage an update. Apply all
    staged updates in one ``update_config_yaml`` call so the YAML write
    is atomic across the whole batch (a bad value rejects the whole
    submission).
    """
    from helpers.config_updater import update_config_yaml

    tunables = _build_tunables()
    pending: Dict[str, Any] = {}
    try:
        for t in tunables:
            raw = request.form.get(t['form_name'])
            if raw is None or raw == '':
                continue
            new_val = t['cast'](raw)
            # Only stage if actually changed (prevents YAML churn on
            # a no-op save).
            if abs(float(new_val) - float(t['current'])) > 1e-9:
                pending[t['key']] = new_val
    except (ValueError, TypeError) as e:
        flash(f"Invalid value: {e}", "danger")
        return redirect(url_for('settings_advanced.index'))

    if not pending:
        flash("No changes to save.", "info")
        return redirect(url_for('settings_advanced.index'))

    try:
        update_config_yaml(pending)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to write advanced settings to config.yaml")
        flash(f"Failed to save: {e}", "danger")
        return redirect(url_for('settings_advanced.index'))

    fields = ', '.join(sorted(pending.keys()))
    flash(
        f"Saved {len(pending)} advanced setting(s): {fields}. "
        "Restart gadget_web.service for changes to take effect.",
        "success",
    )
    return redirect(url_for('settings_advanced.index'))


@settings_advanced_bp.route('/reset', methods=['POST'])
def reset():
    """Reset a single tunable back to its factory default."""
    from helpers.config_updater import update_config_yaml

    form_name = request.form.get('form_name', '')
    tunable = next(
        (t for t in _build_tunables() if t['form_name'] == form_name),
        None,
    )
    if tunable is None:
        flash("Unknown setting.", "danger")
        return redirect(url_for('settings_advanced.index'))

    try:
        update_config_yaml({tunable['key']: tunable['default']})
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to reset advanced setting %s", tunable['key'])
        flash(f"Failed to reset: {e}", "danger")
        return redirect(url_for('settings_advanced.index'))

    flash(
        f"Reset '{tunable['label']}' to default ({tunable['default']} "
        f"{tunable['unit']}). Restart gadget_web.service to apply.",
        "success",
    )
    return redirect(url_for('settings_advanced.index'))
