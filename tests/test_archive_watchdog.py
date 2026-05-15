"""Tests for the Phase 2c archive watchdog + retention prune (issue #76).

Coverage matches the issue spec:

* TestArchiveWatchdogLifecycle      — start, stop, idempotent
* TestArchiveWatchdogSeverity       — every branch of _classify_severity
* TestArchiveWatchdogDiskSpace      — synthetic disk_usage drives warn/crit
* TestArchiveWatchdogReporting      — get_health() / get_status() shape
* TestArchiveRetention              — prune deletes mp4 by mtime, preserves
                                      .dead_letter, calls purge_deleted_videos,
                                      DOES NOT delete trips/waypoints/events
                                      (the May 7 contract)

The severity classifier is a pure function (`_classify_severity`) so most
branches are tested without mocking the DB or filesystem at all.
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from services import archive_queue
from services import archive_watchdog
from services import archive_worker
from services import task_coordinator
from services.archive_queue import enqueue_for_archive
from services.mapping_service import _init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Initialize a fresh geodata.db with the v10 schema (incl. archive_queue)."""
    db_path = str(tmp_path / "geodata.db")
    _init_db(db_path).close()
    return db_path


@pytest.fixture
def archive_root(tmp_path):
    p = tmp_path / "ArchivedClips"
    p.mkdir()
    return str(p)


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Stop watchdog + worker + reset coordinator state between tests."""
    archive_watchdog.stop_watchdog(timeout=5.0)
    archive_worker.stop_worker(timeout=5.0)
    archive_worker._disk_space_pause_until = 0.0
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0
    # Reset watchdog module state so each test starts clean.
    archive_watchdog._last_health = {
        'severity': 'ok',
        'message': 'Archive watchdog has not yet run.',
        'last_successful_copy_at': None,
        'last_successful_copy_age_seconds': None,
        'worker_running': False,
        'paused': False,
        'dead_letter_count': 0,
        'pending_count': 0,
        'disk_free_mb': 0,
        'disk_warning': False,
        'checked_at': None,
    }
    archive_watchdog._retention_state = {
        'last_prune_at': None,
        'last_prune_deleted': 0,
        'last_prune_freed_bytes': 0,
        'last_prune_kept_unsynced': 0,
        'last_prune_error': None,
        'next_prune_due_at': None,
    }
    # Issue #91 — reset duplicate-trigger guard so a test that
    # exercises the short-circuit path doesn't leak the True flag
    # into the next test.
    archive_watchdog._retention_running = False
    yield
    archive_watchdog.stop_watchdog(timeout=5.0)
    archive_worker.stop_worker(timeout=5.0)
    archive_worker._disk_space_pause_until = 0.0
    with task_coordinator._lock:
        task_coordinator._current_task = None
        task_coordinator._task_started = 0.0
        task_coordinator._waiter_count = 0
    archive_watchdog._retention_running = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, total: int, used: int, free: int):
        self.total = total
        self.used = used
        self.free = free


def _fake_usage(free_mb: int, total_mb: int = 32_000) -> _FakeUsage:
    return _FakeUsage(
        total=total_mb * 1024 * 1024,
        used=max(total_mb - free_mb, 0) * 1024 * 1024,
        free=free_mb * 1024 * 1024,
    )


def _make_archive_mp4(root: str, rel: str, *, mtime: float,
                      size: int = 100) -> str:
    # Normalize the rel path so subsequent string-comparison assertions
    # match regardless of which path separator the caller used.
    full = os.path.normpath(os.path.join(root, rel))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as f:
        f.write(b"X" * size)
    os.utime(full, (mtime, mtime))
    return full


# ---------------------------------------------------------------------------
# TestArchiveWatchdogLifecycle
# ---------------------------------------------------------------------------


class TestArchiveWatchdogLifecycle:
    def test_start_returns_true_first_time(self, db, archive_root):
        ok = archive_watchdog.start_watchdog(
            db, archive_root, check_interval_seconds=0.1,
        )
        assert ok is True
        assert archive_watchdog.is_running() is True
        assert archive_watchdog.stop_watchdog(timeout=5) is True
        assert archive_watchdog.is_running() is False

    def test_double_start_is_noop(self, db, archive_root):
        assert archive_watchdog.start_watchdog(
            db, archive_root, check_interval_seconds=0.1,
        ) is True
        assert archive_watchdog.start_watchdog(
            db, archive_root, check_interval_seconds=0.1,
        ) is False
        archive_watchdog.stop_watchdog(timeout=5)

    def test_stop_when_not_running_returns_true(self):
        assert archive_watchdog.stop_watchdog(timeout=2) is True

    def test_wake_does_not_crash_when_not_running(self):
        # wake() must never raise — it's safe to call from any thread.
        archive_watchdog.wake()

    def test_loop_runs_at_least_once(self, db, archive_root):
        archive_watchdog.start_watchdog(
            db, archive_root, check_interval_seconds=0.05,
        )
        try:
            # Wait briefly for first tick to populate _last_health.
            for _ in range(50):
                snap = archive_watchdog.get_health()
                if snap.get('checked_at') is not None:
                    break
                time.sleep(0.05)
            snap = archive_watchdog.get_health()
            assert snap['checked_at'] is not None
            assert snap['severity'] in ('ok', 'warning', 'error', 'critical')
        finally:
            archive_watchdog.stop_watchdog(timeout=5)


# ---------------------------------------------------------------------------
# TestArchiveWatchdogSeverity (acceptance criterion 6 — pure function)
# ---------------------------------------------------------------------------


class TestArchiveWatchdogSeverity:
    """Drive every branch of `_classify_severity` without filesystem/DB."""

    def test_ok_when_no_pending(self):
        sev, msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=0,
            last_copy_age_seconds=None,
            disk_free_mb=10_000,
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'ok'
        assert 'idle' in msg.lower()

    def test_ok_when_recent_copy_and_pending(self):
        sev, _msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=3,
            last_copy_age_seconds=60,  # 1 min — fresh
            disk_free_mb=10_000,
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'ok'

    def test_warning_at_5_min_stale_with_pending(self):
        sev, msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=2,
            last_copy_age_seconds=6 * 60,  # 6 min
            disk_free_mb=10_000,
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'warning'
        assert 'slow' in msg.lower() or 'min' in msg.lower()

    def test_error_at_10_min_stale_with_pending(self):
        # Acceptance criterion 6: 10 min trigger — banner-worthy.
        # Issue #180 — wording was toned down from the alarmist
        # "may be stalled — videos may be lost!" to a neutral
        # "not making progress" since 10 min without a copy is the
        # normal signature of a load-pause under heavy backlog,
        # not yet an emergency. CRITICAL (20 min+) keeps the loud
        # "STALLED ... videos are being lost!" wording.
        sev, msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=5,
            last_copy_age_seconds=15 * 60,  # 15 min
            disk_free_mb=10_000,
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'error'
        assert 'no copy in' in msg.lower()
        assert 'min' in msg.lower()
        assert '5 queued' in msg.lower()

    def test_critical_at_20_min_stale_with_pending(self):
        sev, msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=10,
            last_copy_age_seconds=25 * 60,  # 25 min
            disk_free_mb=10_000,
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'critical'
        assert 'stalled' in msg.lower() or 'lost' in msg.lower()

    def test_critical_when_worker_dead_with_pending(self):
        sev, msg = archive_watchdog._classify_severity(
            worker_running=False,
            pending_count=4,
            last_copy_age_seconds=30,  # would otherwise be ok
            disk_free_mb=10_000,
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'critical'
        assert 'not running' in msg.lower()

    def test_worker_dead_but_no_pending_is_ok(self):
        # No pending work + no worker is fine (e.g., disabled subsystem).
        sev, _msg = archive_watchdog._classify_severity(
            worker_running=False,
            pending_count=0,
            last_copy_age_seconds=None,
            disk_free_mb=10_000,
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'ok'

    def test_disk_warning_when_otherwise_ok(self):
        sev, msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=0,
            last_copy_age_seconds=None,
            disk_free_mb=300,  # < 500 MB
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'warning'
        assert '300' in msg or 'low' in msg.lower()

    def test_disk_critical_overrides_stale_warning(self):
        # Stale = warning, disk = critical → final severity = critical.
        sev, msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=2,
            last_copy_age_seconds=6 * 60,  # warning
            disk_free_mb=50,  # critical
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'critical'
        assert 'critical' in msg.lower()

    def test_stale_critical_overrides_disk_warning(self):
        # Stale = critical, disk = warning → final = critical (stale wins).
        sev, _msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=2,
            last_copy_age_seconds=25 * 60,  # critical
            disk_free_mb=300,  # warning
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'critical'

    def test_equal_severity_combines_messages(self):
        # Both warning → message should contain both halves.
        sev, msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=2,
            last_copy_age_seconds=6 * 60,
            disk_free_mb=300,
            disk_warning_mb=500,
            disk_critical_mb=100,
        )
        assert sev == 'warning'
        # Message has staleness AND disk info combined.
        assert 'slow' in msg.lower()
        assert '300' in msg

    def test_severity_thresholds_are_5_10_20_minutes(self):
        # Verify the literal threshold constants the issue spec mandates.
        assert archive_watchdog._STALE_WARNING_SECONDS == 5 * 60
        assert archive_watchdog._STALE_ERROR_SECONDS == 10 * 60
        assert archive_watchdog._STALE_CRITICAL_SECONDS == 20 * 60

    def test_disk_known_false_skips_disk_overlay(self):
        # Regression: PR #90 reviewer Info #1.
        # When disk_usage stat fails (disk_known=False), the disk
        # overlay must be skipped entirely so a transient OSError
        # does NOT escalate severity to 'critical' via disk_free_mb=0.
        sev, msg = archive_watchdog._classify_severity(
            worker_running=True,
            pending_count=0,
            last_copy_age_seconds=None,
            disk_free_mb=0,            # would normally be < critical
            disk_warning_mb=500,
            disk_critical_mb=100,
            disk_known=False,          # OSError happened
        )
        assert sev == 'ok'
        assert 'CRITICAL' not in msg
        assert '0 MB' not in msg

    def test_disk_known_false_does_not_mask_stale_critical(self):
        # disk_known=False must not suppress a real staleness-driven
        # critical: the worker is dead with pending work — the user
        # MUST see that banner regardless of disk-stat health.
        sev, msg = archive_watchdog._classify_severity(
            worker_running=False,
            pending_count=4,
            last_copy_age_seconds=30,
            disk_free_mb=0,
            disk_warning_mb=500,
            disk_critical_mb=100,
            disk_known=False,
        )
        assert sev == 'critical'
        assert 'not running' in msg.lower()


# ---------------------------------------------------------------------------
# TestArchiveWatchdogDiskSpace
# ---------------------------------------------------------------------------


class TestArchiveWatchdogDiskSpace:
    def test_disk_thresholds_default_to_500_and_100(self, monkeypatch):
        # Force the config import to fail.
        import builtins
        real_import = builtins.__import__

        def _fail_import(name, *a, **kw):
            if name == 'config':
                raise ImportError("simulated")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, '__import__', _fail_import)
        warn_mb, crit_mb = archive_watchdog._resolve_disk_thresholds()
        assert (warn_mb, crit_mb) == (500, 100)

    def test_compute_health_with_low_disk_yields_warning(
        self, db, archive_root, monkeypatch,
    ):
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=300),
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['severity'] == 'warning'
        assert snap['disk_free_mb'] == 300
        assert snap['disk_total_mb'] == 32_000

    def test_compute_health_with_critical_disk_yields_critical(
        self, db, archive_root, monkeypatch,
    ):
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=50),
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['severity'] == 'critical'
        assert snap['disk_free_mb'] == 50

    def test_compute_health_returns_zero_when_archive_root_missing(
        self, db, tmp_path,
    ):
        missing = str(tmp_path / "nonexistent_archive_root")
        snap = archive_watchdog._compute_health(db, missing)
        # Disk fields default to 0 for backward-compat with the JSON
        # payload, but ``disk_known`` is False so the disk overlay was
        # skipped (i.e. severity was NOT escalated to 'critical' on a
        # transient stat failure).
        assert snap['disk_free_mb'] == 0
        assert snap['disk_total_mb'] == 0
        assert snap['disk_known'] is False
        # The disk overlay was skipped — severity must not be 'critical'
        # purely because disk_free_mb=0.
        assert snap['severity'] in ('ok', 'warning', 'error')

    def test_oserror_does_not_escalate_to_disk_critical(
        self, db, archive_root, monkeypatch,
    ):
        # Regression: PR #90 reviewer Info #1.
        # When ``shutil.disk_usage`` raises OSError (transient FS hiccup),
        # the watchdog must NOT report a misleading "0 MB free, CRITICAL"
        # banner. Worker fails open on OSError; watchdog now matches.
        def _raise(_p):
            raise OSError("transient stat failure")
        monkeypatch.setattr(archive_watchdog.shutil, 'disk_usage', _raise)
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['disk_known'] is False
        # Severity comes from staleness (idle queue ⇒ ok), not disk.
        assert snap['severity'] == 'ok'
        # Critical-disk message must NOT appear.
        assert 'CRITICAL' not in snap['message']
        assert '0 MB' not in snap['message']


# ---------------------------------------------------------------------------
# TestArchiveWatchdogReporting (issue spec — get_health/get_status shape)
# ---------------------------------------------------------------------------


class TestArchiveWatchdogReporting:
    REQUIRED_HEALTH_FIELDS = {
        'severity', 'message', 'last_successful_copy_at',
        'last_successful_copy_age_seconds', 'worker_running', 'paused',
        'dead_letter_count', 'pending_count', 'disk_free_mb',
        'disk_total_mb', 'disk_used_mb', 'disk_warning',
        'disk_warning_mb', 'disk_critical_mb', 'disk_known', 'checked_at',
    }

    def test_get_health_shape(self, db, archive_root, monkeypatch):
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=10_000),
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        # _compute_health populates the same fields get_health serves.
        for f in self.REQUIRED_HEALTH_FIELDS:
            assert f in snap, f"missing field {f}"
        assert snap['severity'] in ('ok', 'warning', 'error', 'critical')
        assert isinstance(snap['disk_free_mb'], int)
        assert isinstance(snap['disk_total_mb'], int)

    def test_get_status_includes_retention_and_running_flag(
        self, db, archive_root, monkeypatch,
    ):
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=10_000),
        )
        archive_watchdog.start_watchdog(
            db, archive_root, check_interval_seconds=0.05,
        )
        try:
            for _ in range(50):
                snap = archive_watchdog.get_status()
                if snap.get('checked_at') is not None:
                    break
                time.sleep(0.05)
            snap = archive_watchdog.get_status()
            assert 'retention' in snap
            assert 'retention_days' in snap['retention']
            assert 'last_prune_at' in snap['retention']
            assert 'next_prune_due_at' in snap['retention']
            assert snap['watchdog_running'] is True
        finally:
            archive_watchdog.stop_watchdog(timeout=5)

    def test_get_health_has_age_when_copy_exists(
        self, db, archive_root, monkeypatch,
    ):
        # Simulate a copied row by inserting directly.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO archive_queue("
                " source_path, dest_path, expected_size, expected_mtime,"
                " status, copied_at, priority, enqueued_at, attempts) "
                "VALUES (?, ?, ?, ?, 'copied', ?, 1, ?, 0)",
                (
                    "/teslacam/RecentClips/x.mp4",
                    os.path.join(archive_root, "RecentClips/x.mp4"),
                    100, time.time() - 30,
                    "2025-01-01T00:00:00+00:00",
                    "2025-01-01T00:00:00+00:00",
                ),
            )
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=10_000),
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['last_successful_copy_at'] == "2025-01-01T00:00:00+00:00"
        assert snap['last_successful_copy_age_seconds'] is not None
        assert snap['last_successful_copy_age_seconds'] > 0


# ---------------------------------------------------------------------------
# TestArchiveWatchdogActionable (#180 follow-up)
#
# The "footage may be lost" banner in base.html is gated on BOTH severity
# (error/critical) AND actionable=True. The principle: don't pop a banner
# the operator has no remedy for. Most ERROR/CRITICAL severities come from
# transient SDIO contention where the worker is doing its best — popping
# a banner asking "what can I do?" is pure annoyance. Only two conditions
# are user-actionable:
#   1. Worker not running while clips are pending → restart service.
#   2. SD-card free space below the CRITICAL threshold → free space.
# ---------------------------------------------------------------------------


class TestArchiveWatchdogActionable:
    def test_idle_worker_with_empty_queue_is_not_actionable(
        self, db, archive_root, monkeypatch,
    ):
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=10_000),
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['actionable'] is False

    def test_running_worker_slow_or_stalled_is_not_actionable(
        self, db, archive_root, monkeypatch,
    ):
        # Insert a "last copied" row 25 minutes ago (well into the
        # CRITICAL stall band) plus a pending row so the watchdog
        # severity escalates to 'critical' / "videos are being lost!".
        # actionable MUST still be False because the worker is
        # running and the operator can't fix it from the web UI.
        with sqlite3.connect(db) as conn:
            old_iso = time.strftime(
                '%Y-%m-%dT%H:%M:%S+00:00',
                time.gmtime(time.time() - 25 * 60),
            )
            conn.execute(
                "INSERT INTO archive_queue("
                " source_path, dest_path, expected_size, expected_mtime,"
                " status, copied_at, priority, enqueued_at, attempts) "
                "VALUES (?, ?, ?, ?, 'copied', ?, 1, ?, 0)",
                (
                    "/teslacam/RecentClips/old.mp4",
                    os.path.join(archive_root, "RecentClips/old.mp4"),
                    100, time.time() - 25 * 60,
                    old_iso, old_iso,
                ),
            )
            conn.execute(
                "INSERT INTO archive_queue("
                " source_path, dest_path, expected_size, expected_mtime,"
                " status, priority, enqueued_at, attempts) "
                "VALUES (?, ?, ?, ?, 'pending', 1, ?, 0)",
                (
                    "/teslacam/RecentClips/new.mp4",
                    os.path.join(archive_root, "RecentClips/new.mp4"),
                    100, time.time(),
                    time.strftime(
                        '%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(),
                    ),
                ),
            )
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=10_000),
        )
        # archive_worker.is_running() defaults to True via the lazy
        # import path; we don't need to start a real worker for this
        # test — the watchdog reads the "is the worker thread alive"
        # bool, which is False here. To exercise the running-but-
        # stalled branch we patch is_running to True.
        monkeypatch.setattr(
            archive_worker, 'is_running', lambda: True,
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['severity'] == 'critical'
        # The banner gate MUST NOT fire just because the worker
        # is slow under load — there's nothing the operator can do.
        assert snap['actionable'] is False, (
            "actionable must be False for stale-only conditions: a "
            "running worker that's just slow gives the operator no "
            "remedy and a banner would be pure noise."
        )

    def test_worker_not_running_with_pending_is_actionable(
        self, db, archive_root, monkeypatch,
    ):
        # Worker thread dead + work queued = restart the service.
        # This IS user-actionable.
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO archive_queue("
                " source_path, dest_path, expected_size, expected_mtime,"
                " status, priority, enqueued_at, attempts) "
                "VALUES (?, ?, ?, ?, 'pending', 1, ?, 0)",
                (
                    "/teslacam/RecentClips/new.mp4",
                    os.path.join(archive_root, "RecentClips/new.mp4"),
                    100, time.time(),
                    time.strftime(
                        '%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(),
                    ),
                ),
            )
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=10_000),
        )
        monkeypatch.setattr(
            archive_worker, 'is_running', lambda: False,
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['severity'] == 'critical'
        assert snap['actionable'] is True

    def test_worker_not_running_with_empty_queue_is_not_actionable(
        self, db, archive_root, monkeypatch,
    ):
        # Worker dead but no work to do — there's no urgency.
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=10_000),
        )
        monkeypatch.setattr(
            archive_worker, 'is_running', lambda: False,
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['actionable'] is False

    def test_disk_critical_is_actionable(
        self, db, archive_root, monkeypatch,
    ):
        # SD card below CRITICAL threshold → user can free space.
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=50),  # < 100 MB threshold
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['severity'] == 'critical'
        assert snap['actionable'] is True

    def test_disk_warning_alone_is_not_actionable(
        self, db, archive_root, monkeypatch,
    ):
        # Below the WARNING threshold but above CRITICAL — heads-up
        # only. The user doesn't need a banner; the System Health
        # card will surface the % full as a yellow row.
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage',
            lambda _p: _fake_usage(free_mb=300),  # < 500, > 100
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['actionable'] is False

    def test_disk_unknown_is_not_actionable(
        self, db, archive_root, monkeypatch,
    ):
        # Transient OSError on shutil.disk_usage → disk_known=False
        # → don't claim disk-critical and definitely don't fire the
        # banner.
        def _boom(_p):
            raise OSError("ENOENT (transient)")
        monkeypatch.setattr(
            archive_watchdog.shutil, 'disk_usage', _boom,
        )
        snap = archive_watchdog._compute_health(db, archive_root)
        assert snap['actionable'] is False

    def test_actionable_field_present_in_every_snapshot(
        self, db, archive_root, monkeypatch,
    ):
        # Contract test: the field must always exist so the JSON
        # endpoint (and the banner JS reading it) never trips on
        # KeyError. Test across several severity bands.
        for free_mb, run in [
            (10_000, True),    # ok
            (300, True),       # disk warn
            (50, True),        # disk crit
            (10_000, False),   # worker dead, queue empty
        ]:
            monkeypatch.setattr(
                archive_watchdog.shutil, 'disk_usage',
                lambda _p, _f=free_mb: _fake_usage(free_mb=_f),
            )
            monkeypatch.setattr(
                archive_worker, 'is_running', lambda _r=run: _r,
            )
            snap = archive_watchdog._compute_health(db, archive_root)
            assert 'actionable' in snap, (
                f"actionable missing from snapshot "
                f"(free_mb={free_mb}, running={run})"
            )
            assert isinstance(snap['actionable'], bool)


# ---------------------------------------------------------------------------
# TestArchiveRetention (issue spec — trip preservation contract)
# ---------------------------------------------------------------------------


class TestArchiveRetention:
    def test_old_files_are_deleted(self, db, archive_root):
        old_mtime = time.time() - (40 * 86400)  # 40 days old
        path = _make_archive_mp4(
            archive_root, "RecentClips/old.mp4", mtime=old_mtime,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 1
        assert not os.path.exists(path)

    def test_new_files_are_kept(self, db, archive_root):
        new_mtime = time.time() - (5 * 86400)  # 5 days old
        path = _make_archive_mp4(
            archive_root, "RecentClips/new.mp4", mtime=new_mtime,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 0
        assert os.path.isfile(path)

    def test_dead_letter_files_are_never_deleted(self, db, archive_root):
        old_mtime = time.time() - (90 * 86400)  # 90 days old
        protected = _make_archive_mp4(
            archive_root, ".dead_letter/forensic.mp4", mtime=old_mtime,
        )
        # And one non-dead-letter old file as a control.
        will_be_pruned = _make_archive_mp4(
            archive_root, "RecentClips/old.mp4", mtime=old_mtime,
        )
        archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert os.path.isfile(protected), \
            ".dead_letter must NEVER be touched by retention prune"
        assert not os.path.exists(will_be_pruned)

    def test_purge_deleted_videos_called_for_each_deleted_mp4(
        self, db, archive_root, monkeypatch,
    ):
        old_mtime = time.time() - (40 * 86400)
        paths = [
            os.path.normpath(_make_archive_mp4(
                archive_root, "RecentClips/a.mp4", mtime=old_mtime,
            )),
            os.path.normpath(_make_archive_mp4(
                archive_root, "RecentClips/b.mp4", mtime=old_mtime,
            )),
        ]
        purged = []
        from services import mapping_service

        def _spy(db_path, *, deleted_paths):
            purged.append([os.path.normpath(p) for p in deleted_paths])
        monkeypatch.setattr(
            mapping_service, 'purge_deleted_videos', _spy,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 2
        # purge_deleted_videos called once per deleted file.
        assert len(purged) == 2
        flat = [p for sub in purged for p in sub]
        assert set(flat) == set(paths)

    def test_trips_and_waypoints_are_NEVER_deleted_by_retention(
        self, db, archive_root,
    ):
        """Hard contract: retention NEVER cascade-deletes trips/waypoints/events.

        See copilot-instructions.md — the May 7 McDonalds-trip data loss.
        ``purge_deleted_videos`` is documented to ONLY delete the
        indexed_files row + NULL out video_path on related rows.
        """
        # Insert a trip + waypoint + detected_event referencing a
        # video we're about to retention-prune. Waypoints store the
        # CANONICAL relative path (e.g. ``RecentClips/<base>``) — NOT
        # the absolute filesystem path. ``purge_deleted_videos``
        # canonical-keys the deleted absolute path and matches against
        # the relative form in the DB.
        old_mtime = time.time() - (40 * 86400)
        path = _make_archive_mp4(
            archive_root, "RecentClips/trip-clip.mp4", mtime=old_mtime,
        )
        # Canonical waypoint video_path uses forward slash (DB convention,
        # platform-independent — RecentClips is the canonical prefix).
        rel_video_path = "RecentClips/trip-clip.mp4"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO trips(start_time, end_time, source_folder) "
                "VALUES ('2025-01-01T10:00:00Z','2025-01-01T11:00:00Z','test')"
            )
            trip_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO waypoints(trip_id, lat, lon, "
                "timestamp, video_path) VALUES (?, 37.0, -122.0, "
                "'2025-01-01T10:00:01Z', ?)",
                (trip_id, rel_video_path),
            )
            conn.execute(
                "INSERT INTO detected_events(trip_id, event_type, "
                "timestamp, lat, lon, video_path) VALUES "
                "(?, 'sentry', '2025-01-01T10:00:01Z', 37.0, -122.0, ?)",
                (trip_id, rel_video_path),
            )
            conn.execute(
                "INSERT INTO indexed_files(file_path, file_size, "
                "indexed_at) VALUES (?, 100, '2025-01-01T10:00:01Z')",
                (path,),
            )

        # Snapshot pre-prune row counts.
        with sqlite3.connect(db) as conn:
            trip_count_before = conn.execute(
                "SELECT COUNT(*) FROM trips").fetchone()[0]
            wpt_count_before = conn.execute(
                "SELECT COUNT(*) FROM waypoints").fetchone()[0]
            evt_count_before = conn.execute(
                "SELECT COUNT(*) FROM detected_events").fetchone()[0]
            idx_count_before = conn.execute(
                "SELECT COUNT(*) FROM indexed_files").fetchone()[0]

        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 1

        with sqlite3.connect(db) as conn:
            trip_count_after = conn.execute(
                "SELECT COUNT(*) FROM trips").fetchone()[0]
            wpt_count_after = conn.execute(
                "SELECT COUNT(*) FROM waypoints").fetchone()[0]
            evt_count_after = conn.execute(
                "SELECT COUNT(*) FROM detected_events").fetchone()[0]
            idx_count_after = conn.execute(
                "SELECT COUNT(*) FROM indexed_files").fetchone()[0]
            wpt_video_path = conn.execute(
                "SELECT video_path FROM waypoints WHERE trip_id=?",
                (trip_id,),
            ).fetchone()[0]
            evt_video_path = conn.execute(
                "SELECT video_path FROM detected_events WHERE trip_id=?",
                (trip_id,),
            ).fetchone()[0]

        # Trip / waypoint / event row counts UNCHANGED.
        assert trip_count_after == trip_count_before, \
            "Retention must NOT delete trips (May 7 contract)"
        assert wpt_count_after == wpt_count_before, \
            "Retention must NOT delete waypoints (May 7 contract)"
        assert evt_count_after == evt_count_before, \
            "Retention must NOT delete detected_events (May 7 contract)"
        # video_path nulled out.
        assert wpt_video_path is None
        assert evt_video_path is None
        # indexed_files row gone.
        assert idx_count_after == idx_count_before - 1

    def test_returns_summary_with_required_fields(self, db, archive_root):
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        for f in ('deleted_count', 'freed_bytes', 'scanned',
                  'cutoff_iso', 'retention_days', 'duration_seconds'):
            assert f in summary
        assert summary['retention_days'] == 30

    def test_force_prune_now_updates_bookkeeping(
        self, db, archive_root, monkeypatch,
    ):
        old_mtime = time.time() - (40 * 86400)
        _make_archive_mp4(
            archive_root, "RecentClips/old.mp4", mtime=old_mtime,
        )
        # force_prune_now reads module state for paths.
        archive_watchdog._db_path = db
        archive_watchdog._archive_root = archive_root
        summary = archive_watchdog.force_prune_now()
        assert summary['deleted_count'] == 1
        snap = archive_watchdog.get_status()
        assert snap['retention']['last_prune_at'] is not None
        assert snap['retention']['last_prune_deleted'] == 1

    def test_force_prune_now_returns_error_when_not_started(self):
        # No paths configured → returns error key, no exception.
        archive_watchdog._db_path = None
        archive_watchdog._archive_root = None
        summary = archive_watchdog.force_prune_now()
        assert 'error' in summary
        assert summary['deleted_count'] == 0

    def test_iter_skips_dead_letter_directory(self, archive_root):
        old = time.time() - (90 * 86400)
        _make_archive_mp4(
            archive_root, "RecentClips/keep.mp4", mtime=old,
        )
        _make_archive_mp4(
            archive_root, ".dead_letter/skip.mp4", mtime=old,
        )
        seen = [p for p, _m, _s in
                archive_watchdog._iter_archive_mp4_files(archive_root)]
        assert any(p.endswith('keep.mp4') for p in seen)
        assert not any('.dead_letter' in p for p in seen), \
            "_iter_archive_mp4_files must not yield .dead_letter contents"


# ---------------------------------------------------------------------------
# Issue #208 — retention prune yields the 'retention' lock between batches
# ---------------------------------------------------------------------------


class TestRetentionLockYield:
    """Pre-fix the prune held the 'retention' task slot for 5+ minutes
    on a 5904-file sweep, blocking the indexer / archive worker and
    triggering hardware watchdog resets. The fix releases & re-acquires
    every N files so other workers get a turn.
    """

    def test_yield_releases_then_reacquires_lock(self):
        # Take the lock manually, then exercise the helper. After the
        # call the helper must own the lock again.
        ok = task_coordinator.acquire_task('retention', wait_seconds=1.0)
        assert ok
        try:
            assert archive_watchdog._yield_retention_lock() is True
            info = task_coordinator.current_task_info()
            assert info['busy'] is True
            assert info['task'] == 'retention'
        finally:
            task_coordinator.release_task('retention')

    def test_prune_yields_every_n_files(
            self, db, archive_root, monkeypatch,
    ):
        # Force a tiny yield batch so the test stays fast.
        monkeypatch.setattr(
            archive_watchdog, '_RETENTION_YIELD_EVERY_N_FILES', 3,
        )
        old = time.time() - (40 * 86400)
        # 7 old files → expect 2 yields (after files 3 and 6).
        for i in range(7):
            _make_archive_mp4(
                archive_root, f"RecentClips/old_{i}.mp4", mtime=old,
            )
        yields = {'count': 0}
        real_yield = archive_watchdog._yield_retention_lock

        def _spy():
            yields['count'] += 1
            return real_yield()

        monkeypatch.setattr(
            archive_watchdog, '_yield_retention_lock', _spy,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 7
        # Floor(7/3) = 2 yields.
        assert yields['count'] == 2

    def test_prune_bails_with_partial_summary_when_reacquire_fails(
            self, db, archive_root, monkeypatch,
    ):
        # Configure tiny batch so we yield after the first file.
        monkeypatch.setattr(
            archive_watchdog, '_RETENTION_YIELD_EVERY_N_FILES', 1,
        )
        # Stub the yield helper to simulate "we released the slot and
        # another worker grabbed it and won't release it within the
        # wait window" — including the actual release so the lock state
        # the prune sees matches the real failure mode.
        def _release_and_fail():
            task_coordinator.release_task('retention')
            return False
        monkeypatch.setattr(
            archive_watchdog, '_yield_retention_lock', _release_and_fail,
        )
        old = time.time() - (40 * 86400)
        for i in range(5):
            _make_archive_mp4(
                archive_root, f"RecentClips/old_{i}.mp4", mtime=old,
            )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        # First file deleted before the yield. Loop bails with a
        # partial summary tagged ``yielded_lost_lock``; the unprocessed
        # files remain on disk for the next prune tick.
        assert summary['deleted_count'] >= 1
        assert summary['deleted_count'] < 5
        assert summary['status'] == 'yielded_lost_lock'
        # The duplicate-trigger guard MUST still be cleared so the
        # next caller is not locked out.
        assert archive_watchdog._retention_running is False
        # And the lock itself must be released so other tasks can run.
        assert task_coordinator.current_task_info()['busy'] is False

    def test_prune_releases_lock_normally_when_no_yield_needed(
            self, db, archive_root, monkeypatch,
    ):
        # Yield batch larger than the file count → no yield should fire.
        monkeypatch.setattr(
            archive_watchdog, '_RETENTION_YIELD_EVERY_N_FILES', 1000,
        )
        called = {'count': 0}

        def _should_not_run():
            called['count'] += 1
            return True
        monkeypatch.setattr(
            archive_watchdog, '_yield_retention_lock', _should_not_run,
        )
        old = time.time() - (40 * 86400)
        for i in range(3):
            _make_archive_mp4(
                archive_root, f"RecentClips/old_{i}.mp4", mtime=old,
            )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 3
        assert called['count'] == 0
        # And the lock must be free at the end.
        assert task_coordinator.current_task_info()['busy'] is False


# ---------------------------------------------------------------------------
# Hard-contract grep (mirrors the archive_worker test pattern)
# ---------------------------------------------------------------------------


class TestNoUSBGadgetCalls:
    """archive_watchdog must NEVER call USB-gadget primitives."""

    def test_no_forbidden_tokens_in_executable_code(self):
        path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), "..", "scripts", "web",
                "services", "archive_watchdog.py",
            )
        )
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        # Strip docstrings and comments to avoid false matches in the
        # explanatory header. We use a simple line-based filter.
        executable_lines = []
        in_triple = False
        triple_marker = None
        for line in src.splitlines():
            stripped = line.lstrip()
            if not in_triple:
                for marker in ('"""', "'''"):
                    if stripped.startswith(marker):
                        in_triple = True
                        triple_marker = marker
                        rest = stripped[len(marker):]
                        if marker in rest:
                            in_triple = False
                            triple_marker = None
                        break
                else:
                    code = line.split('#', 1)[0]
                    executable_lines.append(code)
            else:
                if triple_marker and triple_marker in line:
                    in_triple = False
                    triple_marker = None
        body = '\n'.join(executable_lines)
        forbidden = [
            'partition_mount_service', 'quick_edit_part2',
            'rebind_usb_gadget', 'losetup', 'nsenter',
        ]
        for tok in forbidden:
            assert tok not in body, (
                f"archive_watchdog.py executable code references forbidden "
                f"token {tok!r} — Phase 2c hard constraint: no USB "
                f"gadget interaction."
            )

    def test_no_delete_from_trips_waypoints_events(self):
        path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), "..", "scripts", "web",
                "services", "archive_watchdog.py",
            )
        )
        with open(path, "r", encoding="utf-8") as f:
            src = f.read().lower()
        for table in ('trips', 'waypoints', 'detected_events'):
            assert f"delete from {table}" not in src, (
                f"archive_watchdog.py must NOT contain DELETE FROM {table}"
                " — May 7 trip-loss contract"
            )


# ---------------------------------------------------------------------------
# TestRetentionRespectsCloudSync (Phase 1, item 1.3)
# ---------------------------------------------------------------------------


def _make_cloud_db(tmp_path):
    """Create a minimal cloud_sync.db matching the production schema."""
    db_path = str(tmp_path / "cloud_sync.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_synced_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                file_size INTEGER,
                file_mtime REAL,
                remote_path TEXT,
                status TEXT DEFAULT 'pending',
                synced_at TEXT,
                retry_count INTEGER DEFAULT 0,
                last_error TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _record_synced(cloud_db, file_path, status='synced'):
    conn = sqlite3.connect(cloud_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cloud_synced_files (file_path, status) VALUES (?, ?)",
            (file_path, status),
        )
        conn.commit()
    finally:
        conn.close()


class TestRetentionRespectsCloudSync:
    """Phase 1 item 1.3 — never delete clips that haven't been backed up.

    When ``delete_unsynced=False`` AND a cloud provider is configured,
    the retention prune walks the archive but skips any file past the
    cutoff that does not have ``status='synced'`` in the cloud DB.
    Surfaces a counter (``kept_unsynced_count``) for the UI.
    """

    @pytest.fixture
    def cloud_db(self, tmp_path):
        return _make_cloud_db(tmp_path)

    def test_unsynced_old_clip_is_kept_when_protection_on(
        self, db, archive_root, cloud_db, monkeypatch,
    ):
        old_mtime = time.time() - (40 * 86400)
        unsynced = _make_archive_mp4(
            archive_root, "SentryClips/2024-01-01_00-00-00/front.mp4",
            mtime=old_mtime,
        )
        # Force "protection ON" + cloud configured + use our test cloud DB.
        monkeypatch.setattr(
            archive_watchdog, '_resolve_delete_unsynced', lambda: False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: True,
        )
        monkeypatch.setattr(
            archive_watchdog, '_resolve_cloud_db_path', lambda: cloud_db,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 0
        assert summary['kept_unsynced_count'] == 1
        assert os.path.isfile(unsynced), (
            "Unsynced clip past retention must be PROTECTED when "
            "delete_unsynced=False"
        )

    def test_synced_old_clip_is_deleted_when_protection_on(
        self, db, archive_root, cloud_db, monkeypatch,
    ):
        old_mtime = time.time() - (40 * 86400)
        synced = _make_archive_mp4(
            archive_root, "SavedClips/2024-01-01_00-00-00/front.mp4",
            mtime=old_mtime,
        )
        _record_synced(cloud_db, synced, status='synced')
        monkeypatch.setattr(
            archive_watchdog, '_resolve_delete_unsynced', lambda: False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: True,
        )
        monkeypatch.setattr(
            archive_watchdog, '_resolve_cloud_db_path', lambda: cloud_db,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 1
        assert summary['kept_unsynced_count'] == 0
        assert not os.path.exists(synced)

    def test_unsynced_old_clip_is_deleted_when_protection_off(
        self, db, archive_root, cloud_db, monkeypatch,
    ):
        old_mtime = time.time() - (40 * 86400)
        unsynced = _make_archive_mp4(
            archive_root, "SentryClips/2024-01-01_00-00-00/front.mp4",
            mtime=old_mtime,
        )
        # Protection OFF — age-only deletion regardless of cloud status.
        monkeypatch.setattr(
            archive_watchdog, '_resolve_delete_unsynced', lambda: True,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: True,
        )
        monkeypatch.setattr(
            archive_watchdog, '_resolve_cloud_db_path', lambda: cloud_db,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 1
        assert summary['kept_unsynced_count'] == 0
        assert not os.path.exists(unsynced)

    def test_no_cloud_configured_skips_check(
        self, db, archive_root, monkeypatch,
    ):
        """Even with delete_unsynced=False, when no provider is
        configured the cloud check is short-circuited and age-only
        deletion proceeds. Otherwise users without cloud sync would
        never see retention work.
        """
        old_mtime = time.time() - (40 * 86400)
        clip = _make_archive_mp4(
            archive_root, "RecentClips/2024-01-01_00-00-00/front.mp4",
            mtime=old_mtime,
        )
        monkeypatch.setattr(
            archive_watchdog, '_resolve_delete_unsynced', lambda: False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: False,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 1
        assert summary['kept_unsynced_count'] == 0
        assert not os.path.exists(clip)

    def test_relative_path_match_works(
        self, db, archive_root, cloud_db, monkeypatch,
    ):
        """``cloud_synced_files`` rows may be stored as paths relative
        to the archive root (legacy / pre-canonicalization). The
        cloud-sync check must match either form.
        """
        old_mtime = time.time() - (40 * 86400)
        clip = _make_archive_mp4(
            archive_root, "SentryClips/2024-01-01_00-00-00/front.mp4",
            mtime=old_mtime,
        )
        rel = os.path.relpath(clip, archive_root).replace(os.sep, '/')
        # Record using the RELATIVE path only.
        _record_synced(cloud_db, rel, status='synced')
        monkeypatch.setattr(
            archive_watchdog, '_resolve_delete_unsynced', lambda: False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: True,
        )
        monkeypatch.setattr(
            archive_watchdog, '_resolve_cloud_db_path', lambda: cloud_db,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 1, (
            "Relative-path row in cloud_synced_files must satisfy the "
            "synced check; otherwise legacy installs would never delete."
        )
        assert summary['kept_unsynced_count'] == 0
        assert not os.path.exists(clip)

    def test_pending_status_is_not_treated_as_synced(
        self, db, archive_root, cloud_db, monkeypatch,
    ):
        old_mtime = time.time() - (40 * 86400)
        clip = _make_archive_mp4(
            archive_root, "SentryClips/2024-01-01_00-00-00/front.mp4",
            mtime=old_mtime,
        )
        # Row exists but status is NOT 'synced'.
        _record_synced(cloud_db, clip, status='pending')
        monkeypatch.setattr(
            archive_watchdog, '_resolve_delete_unsynced', lambda: False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: True,
        )
        monkeypatch.setattr(
            archive_watchdog, '_resolve_cloud_db_path', lambda: cloud_db,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['deleted_count'] == 0
        assert summary['kept_unsynced_count'] == 1
        assert os.path.isfile(clip)

    def test_summary_includes_metadata_keys(
        self, db, archive_root, monkeypatch,
    ):
        monkeypatch.setattr(
            archive_watchdog, '_resolve_delete_unsynced', lambda: True,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: False,
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        # Every summary must carry the new fields (zero-valued is fine).
        assert 'kept_unsynced_count' in summary
        assert 'delete_unsynced' in summary
        assert 'cloud_configured' in summary

    def test_get_status_surfaces_toggle_state(
        self, db, archive_root, monkeypatch,
    ):
        monkeypatch.setattr(
            archive_watchdog, '_resolve_delete_unsynced', lambda: False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: True,
        )
        archive_watchdog.start_watchdog(
            db, archive_root, check_interval_seconds=60.0,
        )
        try:
            status = archive_watchdog.get_status()
            assert status['retention']['delete_unsynced'] is False
            assert status['retention']['cloud_configured'] is True
            assert 'last_prune_kept_unsynced' in status['retention']
        finally:
            archive_watchdog.stop_watchdog(timeout=5.0)


class TestResolveDeleteUnsynced:
    """Phase 1 item 1.3 — auto-default resolution when YAML key is unset."""

    def test_none_with_cloud_configured_protects(self, monkeypatch):
        # Patch via sys.modules so the lazy `from config import` sees them.
        import config as cfg_module
        monkeypatch.setattr(
            cfg_module, 'CLOUD_ARCHIVE_DELETE_UNSYNCED', None,
            raising=False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: True,
        )
        assert archive_watchdog._resolve_delete_unsynced() is False

    def test_none_without_cloud_configured_age_only(self, monkeypatch):
        import config as cfg_module
        monkeypatch.setattr(
            cfg_module, 'CLOUD_ARCHIVE_DELETE_UNSYNCED', None,
            raising=False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: False,
        )
        assert archive_watchdog._resolve_delete_unsynced() is True

    def test_explicit_true_overrides_cloud_configured(self, monkeypatch):
        import config as cfg_module
        monkeypatch.setattr(
            cfg_module, 'CLOUD_ARCHIVE_DELETE_UNSYNCED', True,
            raising=False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: True,
        )
        assert archive_watchdog._resolve_delete_unsynced() is True

    def test_explicit_false_overrides_no_cloud(self, monkeypatch):
        import config as cfg_module
        monkeypatch.setattr(
            cfg_module, 'CLOUD_ARCHIVE_DELETE_UNSYNCED', False,
            raising=False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: False,
        )
        assert archive_watchdog._resolve_delete_unsynced() is False


class TestResolveRetentionDays:
    """Phase 3a.2 (#98) — verify the unified ``cleanup`` config section
    takes precedence over the legacy ``cloud_archive.archived_clips_retention_days``
    and ``archive.retention_days`` keys, while preserving full backward
    compat for existing installs that haven't migrated yet.

    Resolution order (first non-zero wins):

    1. ``cleanup.policies.ArchivedClips.retention_days``
    2. ``cleanup.default_retention_days``
    3. ``cloud_archive.archived_clips_retention_days``
    4. ``archive.retention_days`` (via ``CLOUD_ARCHIVE_RETENTION_DAYS`` fallback)
    5. Hard-coded ``30``
    """

    def _patch_config(self, monkeypatch, **values):
        """Apply each kwarg to the loaded ``config`` module via monkeypatch.

        Use ``raising=False`` so we can null-out attributes that may not
        exist on every test installation. Also points ``CONFIG_YAML`` at
        a nonexistent path so the Phase 3a.2 YAML-direct read in
        ``_resolve_retention_days`` falls through to the cached config
        attributes that this helper actually controls.
        """
        import config as cfg_module
        monkeypatch.setattr(
            cfg_module, 'CONFIG_YAML',
            '/nonexistent/test/config.yaml', raising=False,
        )
        for k, v in values.items():
            monkeypatch.setattr(cfg_module, k, v, raising=False)

    def test_per_folder_override_wins(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            CLEANUP_POLICIES={'ArchivedClips': {'retention_days': 14, 'enabled': True}},
            CLEANUP_DEFAULT_RETENTION_DAYS=60,
            CLOUD_ARCHIVE_RETENTION_DAYS=90,
        )
        assert archive_watchdog._resolve_retention_days() == 14

    def test_default_used_when_no_override(self, monkeypatch):
        self._patch_config(
            monkeypatch,
            CLEANUP_POLICIES={},
            CLEANUP_DEFAULT_RETENTION_DAYS=60,
            CLOUD_ARCHIVE_RETENTION_DAYS=90,
        )
        assert archive_watchdog._resolve_retention_days() == 60

    def test_default_used_when_archived_override_missing_days(self, monkeypatch):
        # Per-folder block exists but lacks retention_days — fall through.
        self._patch_config(
            monkeypatch,
            CLEANUP_POLICIES={'ArchivedClips': {'enabled': True}},
            CLEANUP_DEFAULT_RETENTION_DAYS=45,
            CLOUD_ARCHIVE_RETENTION_DAYS=90,
        )
        assert archive_watchdog._resolve_retention_days() == 45

    def test_legacy_cloud_archive_used_when_cleanup_empty(self, monkeypatch):
        # Backward-compat path: install with no cleanup.* section but
        # an existing cloud_archive.archived_clips_retention_days.
        self._patch_config(
            monkeypatch,
            CLEANUP_POLICIES={},
            CLEANUP_DEFAULT_RETENTION_DAYS=0,
            CLOUD_ARCHIVE_RETENTION_DAYS=90,
        )
        assert archive_watchdog._resolve_retention_days() == 90

    def test_hardcoded_default_when_everything_missing(self, monkeypatch):
        # All three sources zero/missing → fall to the hard 30-day floor
        # so a misconfigured install never accidentally pretends "no
        # retention" (which would let the SD card fill until OOM).
        self._patch_config(
            monkeypatch,
            CLEANUP_POLICIES={},
            CLEANUP_DEFAULT_RETENTION_DAYS=0,
            CLOUD_ARCHIVE_RETENTION_DAYS=0,
        )
        assert archive_watchdog._resolve_retention_days() == 30

    def test_zero_override_does_not_disable_retention(self, monkeypatch):
        # A user setting retention to 0 in the per-folder UI must NOT
        # be interpreted as "infinite retention" — it falls through to
        # the next source so the system keeps pruning. (Disabling is a
        # separate concept handled by the per-folder ``enabled`` flag.)
        self._patch_config(
            monkeypatch,
            CLEANUP_POLICIES={'ArchivedClips': {'retention_days': 0}},
            CLEANUP_DEFAULT_RETENTION_DAYS=21,
            CLOUD_ARCHIVE_RETENTION_DAYS=90,
        )
        assert archive_watchdog._resolve_retention_days() == 21

    def test_yaml_direct_read_wins_over_cached_attrs(self, monkeypatch, tmp_path):
        # Phase 3a.2 PR #124 review fix: ``_resolve_retention_days`` now
        # reads ``config.yaml`` directly on every call so a save from
        # the Settings UI takes effect without restart. Verify the
        # direct read is preferred over the cached config attributes
        # (which would otherwise lag behind by a service restart).
        cfg_path = tmp_path / 'config.yaml'
        cfg_path.write_text(
            "cleanup:\n"
            "  default_retention_days: 99\n"
            "  policies: {}\n"
            "cloud_archive:\n"
            "  archived_clips_retention_days: 7\n"
        )
        import config as cfg_module
        monkeypatch.setattr(cfg_module, 'CONFIG_YAML', str(cfg_path), raising=False)
        # Cached attrs say 7; direct YAML read says 99. The fresh value wins.
        monkeypatch.setattr(cfg_module, 'CLEANUP_DEFAULT_RETENTION_DAYS', 7, raising=False)
        monkeypatch.setattr(cfg_module, 'CLOUD_ARCHIVE_RETENTION_DAYS', 7, raising=False)
        monkeypatch.setattr(cfg_module, 'CLEANUP_POLICIES', {}, raising=False)
        assert archive_watchdog._resolve_retention_days() == 99

    def test_yaml_direct_read_per_folder_override_wins(self, monkeypatch, tmp_path):
        cfg_path = tmp_path / 'config.yaml'
        cfg_path.write_text(
            "cleanup:\n"
            "  default_retention_days: 60\n"
            "  policies:\n"
            "    ArchivedClips:\n"
            "      enabled: true\n"
            "      retention_days: 14\n"
            "cloud_archive:\n"
            "  archived_clips_retention_days: 90\n"
        )
        import config as cfg_module
        monkeypatch.setattr(cfg_module, 'CONFIG_YAML', str(cfg_path), raising=False)
        assert archive_watchdog._resolve_retention_days() == 14

    def test_yaml_falls_through_to_cloud_archive_when_cleanup_zero(self, monkeypatch, tmp_path):
        cfg_path = tmp_path / 'config.yaml'
        cfg_path.write_text(
            "cleanup:\n"
            "  default_retention_days: 0\n"
            "  policies: {}\n"
            "cloud_archive:\n"
            "  archived_clips_retention_days: 21\n"
        )
        import config as cfg_module
        monkeypatch.setattr(cfg_module, 'CONFIG_YAML', str(cfg_path), raising=False)
        assert archive_watchdog._resolve_retention_days() == 21

    def test_yaml_falls_through_to_archive_legacy_key(self, monkeypatch, tmp_path):
        cfg_path = tmp_path / 'config.yaml'
        cfg_path.write_text(
            "cleanup:\n"
            "  default_retention_days: 0\n"
            "  policies: {}\n"
            "archive:\n"
            "  retention_days: 45\n"
        )
        import config as cfg_module
        monkeypatch.setattr(cfg_module, 'CONFIG_YAML', str(cfg_path), raising=False)
        assert archive_watchdog._resolve_retention_days() == 45


# ---------------------------------------------------------------------------
# Issue #91 — duplicate-trigger guard for retention prune
# ---------------------------------------------------------------------------


class TestRetentionRunningGuard:
    """Issue #91: a second concurrent caller of ``_run_retention_prune``
    (e.g. Settings UI ``Prune now`` click landing while the watchdog
    tick is mid-walk, OR ``archive_worker._maybe_trigger_critical_cleanup``
    spawns a daemon thread that races a UI click) must NOT block the
    request thread for up to 60 s on
    ``task_coordinator.acquire_task('retention', wait_seconds=60.0)``.

    The fix is a module-level ``_retention_running`` boolean flag set
    BEFORE ``acquire_task`` and cleared in the outer ``finally``. A
    second caller sees the flag and short-circuits with a summary
    carrying ``status='already_running'``.
    """

    def test_short_circuit_returns_already_running_status(
        self, db, archive_root, monkeypatch,
    ):
        # Pre-set the flag to simulate an in-flight prune.
        archive_watchdog._retention_running = True

        # Spy on task_coordinator.acquire_task — the short-circuit
        # MUST happen BEFORE we touch the coordinator. If the spy is
        # called, the guard is broken.
        called = []

        def spy_acquire(*a, **kw):
            called.append((a, kw))
            return True

        monkeypatch.setattr(
            archive_watchdog.task_coordinator, 'acquire_task', spy_acquire,
        )

        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )

        assert summary.get('status') == 'already_running', (
            "When _retention_running is True, the function must return "
            "a summary with status='already_running'."
        )
        assert called == [], (
            "Short-circuited callers must NOT call task_coordinator."
            "acquire_task — that's the whole point of the guard."
        )
        assert summary['deleted_count'] == 0
        assert summary['scanned'] == 0
        # Flag must remain True — we faked the in-flight prune; the
        # real one (which set it) is still expected to clear it.
        assert archive_watchdog._retention_running is True

    def test_flag_cleared_on_normal_completion(
        self, db, archive_root, monkeypatch,
    ):
        old = time.time() - (60 * 86400)
        _make_archive_mp4(archive_root, "RecentClips/old.mp4", mtime=old)
        # Sanity: flag starts False.
        assert archive_watchdog._retention_running is False
        archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert archive_watchdog._retention_running is False, (
            "Flag must be cleared after a normal run so the next "
            "caller can proceed."
        )

    def test_flag_cleared_on_exception(
        self, db, archive_root, monkeypatch,
    ):
        old = time.time() - (60 * 86400)
        _make_archive_mp4(archive_root, "RecentClips/old.mp4", mtime=old)

        def boom(*a, **kw):
            raise RuntimeError("synthetic walk failure")

        monkeypatch.setattr(
            archive_watchdog, '_iter_archive_mp4_files', boom,
        )
        with pytest.raises(RuntimeError, match="synthetic"):
            archive_watchdog._run_retention_prune(
                archive_root, db, retention_days=30,
            )
        assert archive_watchdog._retention_running is False, (
            "Flag must be released even when the walk raises — "
            "otherwise a single failed prune would lock out every "
            "subsequent attempt forever."
        )

    def test_flag_cleared_when_acquire_task_fails(
        self, db, archive_root, monkeypatch,
    ):
        # acquire_task returns False (e.g. another heavy task is
        # holding the slot) — the function returns without doing
        # work, but MUST still clear the flag.
        monkeypatch.setattr(
            archive_watchdog.task_coordinator, 'acquire_task',
            lambda *a, **kw: False,
        )
        # release_task should NOT be called when acquire_task returned
        # False — guard against a regression that calls release on a
        # slot we never acquired.
        released = []
        monkeypatch.setattr(
            archive_watchdog.task_coordinator, 'release_task',
            lambda name: released.append(name),
        )
        summary = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert summary['scanned'] == 0
        assert summary['deleted_count'] == 0
        # Flag must be cleared so the next call can try again.
        assert archive_watchdog._retention_running is False
        # And release_task must NOT have been called for a slot we
        # never held.
        assert released == [], (
            f"release_task called for {released!r} despite "
            f"acquire_task returning False"
        )

    def test_force_prune_now_returns_status_when_short_circuited(
        self, db, archive_root, monkeypatch,
    ):
        archive_watchdog._db_path = db
        archive_watchdog._archive_root = archive_root
        archive_watchdog._retention_running = True
        # Snapshot bookkeeping so we can prove it's not overwritten.
        snap_before = dict(archive_watchdog._retention_state)

        summary = archive_watchdog.force_prune_now()
        assert summary.get('status') == 'already_running'

        # CRITICAL: bookkeeping must NOT be touched on short-circuit —
        # otherwise the in-flight first run's eventual results would
        # be silently overwritten with zeros.
        snap_after = dict(archive_watchdog._retention_state)
        assert snap_after == snap_before, (
            f"_retention_state was mutated on short-circuit: "
            f"{snap_before!r} -> {snap_after!r}. "
            f"Bookkeeping updates must be skipped when "
            f"status='already_running'."
        )

    def test_maybe_run_retention_skips_bookkeeping_on_short_circuit(
        self, db, archive_root, monkeypatch,
    ):
        # Make the watchdog tick think the prune is due.
        archive_watchdog._retention_state['next_prune_due_at'] = (
            time.time() - 1.0
        )
        archive_watchdog._retention_running = True
        snap_before = dict(archive_watchdog._retention_state)

        archive_watchdog._maybe_run_retention(archive_root, db)

        snap_after = dict(archive_watchdog._retention_state)
        assert snap_after == snap_before, (
            "Watchdog tick must not advance next_prune_due_at or "
            "touch any other bookkeeping when the prune was "
            "short-circuited; otherwise the in-flight prune's "
            "eventual results would be lost."
        )

    def test_two_sequential_calls_both_succeed(
        self, db, archive_root, monkeypatch,
    ):
        """Sanity: the guard does not break repeat-after-completion."""
        old = time.time() - (60 * 86400)
        _make_archive_mp4(archive_root, "RecentClips/a.mp4", mtime=old)
        s1 = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert s1['deleted_count'] == 1
        assert 'status' not in s1

        # Second sequential call (after the first cleared the flag)
        # must run normally — not be falsely treated as a duplicate.
        _make_archive_mp4(archive_root, "RecentClips/b.mp4", mtime=old)
        s2 = archive_watchdog._run_retention_prune(
            archive_root, db, retention_days=30,
        )
        assert s2['deleted_count'] == 1
        assert 'status' not in s2

    def test_concurrent_threaded_call_short_circuits(
        self, db, archive_root, monkeypatch,
    ):
        """Two real threads — verify only one runs the walk and the
        other observes ``status='already_running'``."""
        import threading
        old = time.time() - (60 * 86400)
        # 5 files so the first walk takes a measurable moment.
        for i in range(5):
            _make_archive_mp4(
                archive_root, f"RecentClips/x{i}.mp4", mtime=old,
            )

        # Slow down the walk so the second caller is guaranteed to
        # arrive while the first is in-flight.
        gate = threading.Event()
        original_iter = archive_watchdog._iter_archive_mp4_files

        def slow_iter(root):
            for item in original_iter(root):
                gate.wait(timeout=2.0)
                yield item

        monkeypatch.setattr(
            archive_watchdog, '_iter_archive_mp4_files', slow_iter,
        )

        results = {}

        def runner(key):
            results[key] = archive_watchdog._run_retention_prune(
                archive_root, db, retention_days=30,
            )

        t1 = threading.Thread(target=runner, args=('first',), daemon=True)
        t1.start()
        # Give t1 a moment to set the flag and enter the walk.
        time.sleep(0.05)
        t2 = threading.Thread(target=runner, args=('second',), daemon=True)
        t2.start()
        # Second call should short-circuit immediately (no acquire_task
        # wait, no walk).
        t2.join(timeout=2.0)
        assert not t2.is_alive(), (
            "Second concurrent caller must short-circuit immediately, "
            "not block on the lock or the walk."
        )
        # Now release the gate so t1 can finish.
        gate.set()
        t1.join(timeout=10.0)
        assert not t1.is_alive()

        assert results['second'].get('status') == 'already_running'
        # First caller did the actual work.
        assert results['first'].get('status') != 'already_running'
        assert results['first']['deleted_count'] == 5
        # Flag must be cleared after the first finishes.
        assert archive_watchdog._retention_running is False


# ---------------------------------------------------------------------------
# TestCapacityPrune — free_space_target_pct + max_archive_size_gb enforcement
# ---------------------------------------------------------------------------


def _patch_capacity_config(monkeypatch, *, free_pct: int, max_gb: int):
    """Helper: monkeypatch the two config resolvers used by capacity prune.

    Patching ``_resolve_*`` (not the underlying ``config`` constants) so
    each test gets exact, deterministic values regardless of the live
    ``config.yaml`` on disk.
    """
    monkeypatch.setattr(
        archive_watchdog, '_resolve_free_space_target_pct',
        lambda: int(free_pct),
    )
    monkeypatch.setattr(
        archive_watchdog, '_resolve_max_archive_size_gb',
        lambda: int(max_gb),
    )


class TestCapacityPrune:
    """``_run_capacity_prune`` enforces the two Settings → Storage knobs.

    * ``free_space_target_pct`` (soft floor — delete oldest when free %
      drops below the target)
    * ``max_archive_size_gb`` (hard cap — delete oldest when total .mp4
      bytes exceed the cap)

    Both must honor:
      - "trips are sacred" via ``purge_deleted_videos`` (only the
        ``indexed_files`` row is dropped; trips/waypoints/events stay)
      - cloud-pending preservation (skip files not yet synced when a
        cloud provider is configured AND ``delete_unsynced=False``)
      - the protected-file guard via ``safe_delete_archive_video``
        (refuses ``*.img`` and any path outside ``archive_root``)
    """

    def test_no_op_when_both_knobs_disabled(
        self, db, archive_root, monkeypatch,
    ):
        # Both 0 → walk is skipped entirely; not even the pre-walk
        # statvfs runs. Verifies the cheap-path on a default config.
        _patch_capacity_config(monkeypatch, free_pct=0, max_gb=0)
        old = time.time() - (10 * 86400)
        for i in range(3):
            _make_archive_mp4(
                archive_root, f"RecentClips/keep_{i}.mp4",
                mtime=old + i, size=1024,
            )
        summary = archive_watchdog._run_capacity_prune(
            archive_root, db,
        )
        assert summary['capacity_deleted_count'] == 0
        assert summary['capacity_scanned'] == 0, (
            "When both knobs are 0 the function MUST short-circuit "
            "BEFORE the walk so a default-config tick is essentially "
            "free."
        )
        assert summary['free_space_target_pct'] == 0
        assert summary['max_archive_size_gb'] == 0

    def test_no_op_when_under_cap_and_above_free_target(
        self, db, archive_root, monkeypatch,
    ):
        # Cap = 1 GB, archive holds ~3 KB. Free space comfortably
        # above the target. Nothing should be deleted.
        _patch_capacity_config(monkeypatch, free_pct=5, max_gb=1)
        # Synthesize a mostly-empty disk (50% free).
        monkeypatch.setattr(
            archive_watchdog, '_safe_disk_usage',
            lambda _p: _fake_usage(free_mb=16_000, total_mb=32_000),
        )
        old = time.time() - (10 * 86400)
        for i in range(3):
            _make_archive_mp4(
                archive_root, f"RecentClips/keep_{i}.mp4",
                mtime=old + i, size=1024,
            )
        summary = archive_watchdog._run_capacity_prune(
            archive_root, db,
        )
        assert summary['capacity_deleted_count'] == 0
        assert summary['capacity_scanned'] == 3, (
            "Walk must run when at least one knob is enabled so the "
            "summary can report current totals."
        )

    def test_max_size_cap_deletes_oldest_first(
        self, db, archive_root, monkeypatch,
    ):
        # Cap = 1 byte (so 4 × 1 KB clips → 4096 bytes is way over).
        # Set the cap by patching the resolver directly to a fractional
        # GB equivalent — but the resolver returns an int, so use a
        # tiny cap_bytes by overriding the resolver to return 1 and
        # intercepting the GB→bytes math via monkeypatch on the file
        # itself. Simplest: write 4 clips totaling 4 GB worth of size
        # via a fake size and use cap=2 GB.
        # Actual approach: override _iter_archive_mp4_files so the size
        # column is the synthetic "GB" we want, and set cap=2 GB.
        _patch_capacity_config(monkeypatch, free_pct=0, max_gb=2)
        # 4 clips at 1 GB each → 4 GB total, cap 2 GB → must delete 2
        # oldest.
        gb = 1024 * 1024 * 1024
        files = []
        base_mtime = time.time() - (10 * 86400)
        for i in range(4):
            p = _make_archive_mp4(
                archive_root, f"RecentClips/clip_{i}.mp4",
                mtime=base_mtime + i, size=10,  # tiny on disk
            )
            # Synthesize size at the iterator layer instead.
            files.append((p, base_mtime + i, gb))
        monkeypatch.setattr(
            archive_watchdog, '_iter_archive_mp4_files',
            lambda _root: iter(files),
        )
        # No disk_usage interference (None → free-space sub-pass off).
        monkeypatch.setattr(
            archive_watchdog, '_safe_disk_usage', lambda _p: None,
        )
        summary = archive_watchdog._run_capacity_prune(
            archive_root, db,
        )
        # Cap is 2 GB; we have 4 GB; must delete 2 oldest to land
        # at 2 GB (cap_bytes <= total).
        assert summary['capacity_deleted_count'] == 2
        # Oldest two (clip_0, clip_1) gone; newer two (clip_2, clip_3)
        # kept.
        assert not os.path.exists(files[0][0])
        assert not os.path.exists(files[1][0])
        assert os.path.exists(files[2][0])
        assert os.path.exists(files[3][0])

    def test_free_space_target_deletes_oldest_first(
        self, db, archive_root, monkeypatch,
    ):
        # Disk: 32 GB total, only 1 GB free → 3.1% < target 20%.
        # 4 clips at 0.5 GB each. Each delete frees 0.5 GB.
        # Need to bring free from 1 GB → 6.4 GB (20% of 32 GB) =
        # +5.4 GB → 11 deletes ... but we only have 4 clips. The
        # loop should stop after deleting all available files.
        # Verify oldest-first ordering on what IS deleted.
        _patch_capacity_config(monkeypatch, free_pct=20, max_gb=0)
        monkeypatch.setattr(
            archive_watchdog, '_safe_disk_usage',
            lambda _p: _fake_usage(free_mb=1_000, total_mb=32_000),
        )
        half_gb = 512 * 1024 * 1024
        files = []
        base_mtime = time.time() - (10 * 86400)
        for i in range(4):
            p = _make_archive_mp4(
                archive_root, f"RecentClips/clip_{i}.mp4",
                mtime=base_mtime + i, size=10,
            )
            files.append((p, base_mtime + i, half_gb))
        monkeypatch.setattr(
            archive_watchdog, '_iter_archive_mp4_files',
            lambda _root: iter(files),
        )
        summary = archive_watchdog._run_capacity_prune(
            archive_root, db,
        )
        # All 4 deleted, oldest-first.
        assert summary['capacity_deleted_count'] == 4
        for p, _m, _s in files:
            assert not os.path.exists(p)

    def test_protected_img_files_are_never_deleted(
        self, db, archive_root, monkeypatch, tmp_path,
    ):
        # safe_delete_archive_video (file_safety) refuses .img by
        # extension AND parent dir == GADGET_DIR. Verify the capacity
        # prune routes through it: feed a .img through the iterator
        # alongside an mp4, point GADGET_DIR at archive_root so the
        # protection guard fires, and confirm the .img survives.
        from services import file_safety
        monkeypatch.setattr(
            file_safety, '_get_gadget_dir', lambda: archive_root,
        )
        _patch_capacity_config(monkeypatch, free_pct=0, max_gb=1)
        gb = 1024 * 1024 * 1024
        img_path = os.path.join(archive_root, "usb_cam.img")
        with open(img_path, 'wb') as f:
            f.write(b"X" * 100)
        mp4_path = _make_archive_mp4(
            archive_root, "RecentClips/old.mp4",
            mtime=time.time() - 86400, size=100,
        )
        # Iterator yields .img first (oldest), then mp4.
        files = [
            (img_path, time.time() - (3 * 86400), 2 * gb),
            (mp4_path, time.time() - 86400, 2 * gb),
        ]
        monkeypatch.setattr(
            archive_watchdog, '_iter_archive_mp4_files',
            lambda _root: iter(files),
        )
        monkeypatch.setattr(
            archive_watchdog, '_safe_disk_usage', lambda _p: None,
        )
        summary = archive_watchdog._run_capacity_prune(
            archive_root, db,
        )
        # .img refused → counted as kept_protected; .mp4 deleted to
        # bring total under cap.
        assert os.path.exists(img_path), (
            ".img files MUST never be deleted by the capacity prune "
            "(safe_delete_archive_video.is_protected_file guard)"
        )
        assert summary['capacity_kept_protected_count'] >= 1

    def test_trips_are_sacred_under_capacity_prune(
        self, db, archive_root, monkeypatch,
    ):
        # Insert a trip + waypoint + detected_event tied to the file
        # we're about to capacity-prune. After the prune: the file
        # is gone, but the trip / waypoint / event rows must still
        # be present (only video_path NULL'd on the linked rows).
        old_mtime = time.time() - (60 * 86400)
        path = _make_archive_mp4(
            archive_root, "RecentClips/sacred.mp4",
            mtime=old_mtime, size=10,
        )
        # mapping_service.purge_deleted_videos matches via
        # canonical_key, which compares basenames + parent dir name.
        # Insert the indexed_files row with the same path so the
        # purge finds it and reconciles waypoints/events.
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO trips "
                "(start_time, end_time, distance_km, duration_seconds) "
                "VALUES (?, ?, ?, ?)",
                ("2026-05-01T00:00:00", "2026-05-01T00:30:00",
                 5.0, 1800),
            )
            trip_id = conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO waypoints "
                "(trip_id, timestamp, lat, lon, video_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (trip_id, "2026-05-01T00:15:00", 40.0, -111.0, path),
            )
            conn.execute(
                "INSERT INTO detected_events "
                "(trip_id, timestamp, lat, lon, event_type, "
                "video_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (trip_id, "2026-05-01T00:15:30", 40.0, -111.0,
                 'hard_brake', path),
            )
            conn.execute(
                "INSERT INTO indexed_files "
                "(file_path, file_size, indexed_at) "
                "VALUES (?, ?, ?)",
                (path, 10, "2026-05-01T00:30:00"),
            )
            conn.commit()
        finally:
            conn.close()
        _patch_capacity_config(monkeypatch, free_pct=0, max_gb=1)
        gb = 1024 * 1024 * 1024
        files = [(path, old_mtime, 2 * gb)]
        monkeypatch.setattr(
            archive_watchdog, '_iter_archive_mp4_files',
            lambda _root: iter(files),
        )
        monkeypatch.setattr(
            archive_watchdog, '_safe_disk_usage', lambda _p: None,
        )
        summary = archive_watchdog._run_capacity_prune(
            archive_root, db,
        )
        assert summary['capacity_deleted_count'] == 1
        assert not os.path.exists(path)
        # Trips + waypoints + events still present.
        conn = sqlite3.connect(db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM trips"
            ).fetchone()[0] == 1, "trips ROW must survive capacity prune"
            assert conn.execute(
                "SELECT COUNT(*) FROM waypoints"
            ).fetchone()[0] == 1, "waypoints ROW must survive capacity prune"
            assert conn.execute(
                "SELECT COUNT(*) FROM detected_events"
            ).fetchone()[0] == 1, "events ROW must survive capacity prune"
            # indexed_files row dropped (its purpose was tracking the
            # file that no longer exists).
            assert conn.execute(
                "SELECT COUNT(*) FROM indexed_files WHERE file_path=?",
                (path,),
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_cloud_pending_files_are_kept(
        self, db, archive_root, monkeypatch,
    ):
        # When a cloud provider is configured AND delete_unsynced=False,
        # an unsynced file MUST be skipped past the capacity threshold
        # and counted in capacity_kept_unsynced_count, not deleted.
        _patch_capacity_config(monkeypatch, free_pct=0, max_gb=1)
        monkeypatch.setattr(
            archive_watchdog, '_resolve_delete_unsynced', lambda: False,
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_cloud_configured', lambda: True,
        )
        monkeypatch.setattr(
            archive_watchdog, '_resolve_cloud_db_path',
            lambda: '/tmp/fake_cloud.db',
        )
        monkeypatch.setattr(
            archive_watchdog, '_is_synced_to_cloud',
            lambda *a, **kw: False,  # nothing is synced
        )
        gb = 1024 * 1024 * 1024
        path = _make_archive_mp4(
            archive_root, "RecentClips/unsynced.mp4",
            mtime=time.time() - 86400, size=10,
        )
        files = [(path, time.time() - 86400, 2 * gb)]
        monkeypatch.setattr(
            archive_watchdog, '_iter_archive_mp4_files',
            lambda _root: iter(files),
        )
        monkeypatch.setattr(
            archive_watchdog, '_safe_disk_usage', lambda _p: None,
        )
        summary = archive_watchdog._run_capacity_prune(
            archive_root, db,
        )
        assert summary['capacity_deleted_count'] == 0
        assert summary['capacity_kept_unsynced_count'] == 1
        assert os.path.exists(path), (
            "Unsynced file MUST be kept past capacity threshold "
            "when cloud is configured + delete_unsynced=False — "
            "extended WiFi outage cannot cause silent footage loss"
        )

    def test_force_prune_now_runs_capacity_pass_after_time_pass(
        self, db, archive_root, monkeypatch,
    ):
        # Wire up the watchdog so force_prune_now has paths set, then
        # verify the returned summary has a 'capacity' key — proving
        # _run_capacity_prune is invoked from force_prune_now.
        archive_watchdog._archive_root = archive_root
        archive_watchdog._db_path = db
        try:
            _patch_capacity_config(monkeypatch, free_pct=0, max_gb=0)
            old = time.time() - (60 * 86400)
            _make_archive_mp4(
                archive_root, "RecentClips/old.mp4",
                mtime=old, size=10,
            )
            summary = archive_watchdog.force_prune_now()
            assert 'capacity' in summary, (
                "force_prune_now must call _run_capacity_prune so the "
                "Settings → Storage page knobs are actually enforced"
            )
            assert summary['capacity']['free_space_target_pct'] == 0
            assert summary['capacity']['max_archive_size_gb'] == 0
        finally:
            archive_watchdog._archive_root = None
            archive_watchdog._db_path = None

    def test_resolve_free_space_target_pct_clamps_invalid(
        self, monkeypatch,
    ):
        # Negative or > 50 → 0 (disabled). Defends against misconfig.
        import config as cfg_mod
        monkeypatch.setattr(
            cfg_mod, 'CLEANUP_FREE_SPACE_TARGET_PCT', -5, raising=False,
        )
        assert archive_watchdog._resolve_free_space_target_pct() == 0
        monkeypatch.setattr(
            cfg_mod, 'CLEANUP_FREE_SPACE_TARGET_PCT', 999, raising=False,
        )
        assert archive_watchdog._resolve_free_space_target_pct() == 0
        monkeypatch.setattr(
            cfg_mod, 'CLEANUP_FREE_SPACE_TARGET_PCT', 25, raising=False,
        )
        assert archive_watchdog._resolve_free_space_target_pct() == 25

    def test_resolve_max_archive_size_gb_clamps_negative(
        self, monkeypatch,
    ):
        import config as cfg_mod
        monkeypatch.setattr(
            cfg_mod, 'CLEANUP_MAX_ARCHIVE_SIZE_GB', -1, raising=False,
        )
        assert archive_watchdog._resolve_max_archive_size_gb() == 0
        monkeypatch.setattr(
            cfg_mod, 'CLEANUP_MAX_ARCHIVE_SIZE_GB', 100, raising=False,
        )
        assert archive_watchdog._resolve_max_archive_size_gb() == 100
