"""Tests for file watcher lifecycle and callback safety.

These tests focus on the lifecycle contract (stop joins, generation
counter drops stale callbacks, restart waits for mounts) rather than
exercising real inotify on the host. They run on Windows and Linux
because the polling fallback is what the lifecycle code relies on.
"""

import os
import threading
import time

import pytest

from services import file_watcher_service as fws


@pytest.fixture(autouse=True)
def _reset_watcher_state():
    """Make each test independent by tearing down any leftover state."""
    fws.stop_watcher(timeout=2.0)
    # Clear callback lists so callbacks from one test don't leak.
    fws._on_new_file_callbacks.clear()
    fws._on_deleted_file_callbacks.clear()
    fws._on_archive_callbacks.clear()
    yield
    fws.stop_watcher(timeout=2.0)
    fws._on_new_file_callbacks.clear()
    fws._on_deleted_file_callbacks.clear()
    fws._on_archive_callbacks.clear()


class TestLifecycle:
    def test_start_returns_true_when_path_valid(self, tmp_path):
        assert fws.start_watcher([str(tmp_path)]) is True
        assert fws.get_watcher_status()["running"] is True

    def test_start_returns_false_when_already_running(self, tmp_path):
        assert fws.start_watcher([str(tmp_path)]) is True
        # Second start while running should be a no-op.
        assert fws.start_watcher([str(tmp_path)]) is False

    def test_start_returns_false_when_no_valid_paths(self, tmp_path):
        # Nonexistent path → watcher refuses to start.
        bogus = tmp_path / "does-not-exist"
        assert fws.start_watcher([str(bogus)]) is False
        assert fws.get_watcher_status()["running"] is False

    def test_stop_joins_thread(self, tmp_path):
        fws.start_watcher([str(tmp_path)])
        # The thread should exit quickly once the stop event is set.
        assert fws.stop_watcher(timeout=5.0) is True
        # And the global handle should be cleared.
        assert fws._watcher_thread is None
        assert fws.get_watcher_status()["running"] is False

    def test_stop_is_idempotent(self, tmp_path):
        fws.start_watcher([str(tmp_path)])
        fws.stop_watcher(timeout=5.0)
        # A second stop must not raise even though no thread exists.
        assert fws.stop_watcher(timeout=1.0) is True

    def test_restart_works(self, tmp_path):
        assert fws.start_watcher([str(tmp_path)]) is True
        first_thread = fws._watcher_thread
        assert fws.restart_watcher([str(tmp_path)],
                                    mount_wait_seconds=2.0) is True
        # Restart must yield a new thread instance.
        assert fws._watcher_thread is not None
        assert fws._watcher_thread is not first_thread


class TestGenerationGuard:
    def test_generation_increments_on_stop(self, tmp_path):
        fws.start_watcher([str(tmp_path)])
        before = fws._watcher_generation
        fws.stop_watcher(timeout=2.0)
        assert fws._watcher_generation == before + 1

    def test_stale_new_file_callbacks_are_dropped(self, tmp_path):
        # Simulate a stale callback batch by capturing the current
        # generation, then bumping it (as stop_watcher would), then
        # invoking _notify_callbacks with the captured value. The
        # callback must NOT fire.
        received = []
        fws.register_callback(lambda paths: received.extend(paths))
        captured = fws._watcher_generation
        fws._watcher_generation = captured + 1  # simulate stop_watcher
        try:
            fws._notify_callbacks(["/some/file.mp4"], my_generation=captured)
        finally:
            fws._watcher_generation = captured
        assert received == []

    def test_current_generation_callbacks_fire(self, tmp_path):
        received = []
        fws.register_callback(lambda paths: received.extend(paths))
        current = fws._watcher_generation
        fws._notify_callbacks(["/a/b.mp4"], my_generation=current)
        assert received == ["/a/b.mp4"]

    def test_stale_delete_callbacks_are_dropped(self):
        deleted = []
        fws.register_delete_callback(lambda paths: deleted.extend(paths))
        captured = fws._watcher_generation
        fws._watcher_generation = captured + 1
        try:
            fws._notify_delete_callbacks(["/x.mp4"], my_generation=captured)
        finally:
            fws._watcher_generation = captured
        assert deleted == []


class TestInotifyParser:
    def test_parses_single_event(self):
        import struct
        wd = 7
        mask = fws._IN_DELETE
        cookie = 0
        name = b'2026-01-01_12-00-00-front.mp4\0\0\0'  # null-padded
        header = struct.pack('iIII', wd, mask, cookie, len(name))
        data = header + name
        wd_map = {7: '/mnt/teslacam/RecentClips'}

        events = list(fws._parse_inotify_events(data, wd_map))
        assert len(events) == 1
        path, returned_mask = events[0]
        assert path == os.path.join(
            '/mnt/teslacam/RecentClips',
            '2026-01-01_12-00-00-front.mp4',
        )
        assert returned_mask == mask

    def test_skips_unknown_wd(self):
        import struct
        # Watch descriptor not in the map (e.g. removed by inotify_rm_watch).
        data = struct.pack('iIII', 999, fws._IN_CREATE, 0, 8) + b'foo.mp4\0'
        events = list(fws._parse_inotify_events(data, wd_map={1: '/x'}))
        assert events == []

    def test_skips_empty_name(self):
        import struct
        # Directory-level events have len=0 and no name. We don't track
        # directories individually, so these should be filtered out.
        data = struct.pack('iIII', 1, fws._IN_DELETE, 0, 0)
        events = list(fws._parse_inotify_events(data, wd_map={1: '/x'}))
        assert events == []

    def test_handles_multiple_events_in_buffer(self):
        import struct
        wd_map = {1: '/dir'}
        # Build two back-to-back events in one buffer.
        ev1 = struct.pack('iIII', 1, fws._IN_CREATE, 0, 8) + b'a.mp4\0\0\0'
        ev2 = struct.pack('iIII', 1, fws._IN_DELETE, 0, 8) + b'b.mp4\0\0\0'
        data = ev1 + ev2

        events = list(fws._parse_inotify_events(data, wd_map))
        assert len(events) == 2
        assert events[0][0].endswith('a.mp4')
        assert events[1][0].endswith('b.mp4')
        assert events[0][1] == fws._IN_CREATE
        assert events[1][1] == fws._IN_DELETE


class TestPollingDeleteDetection:
    """The polling fallback synthesizes delete events by diffing the
    known_files set against the filesystem. Verify that signal flows to
    registered callbacks."""

    def test_polling_loop_detects_deleted_file(self, tmp_path,
                                                monkeypatch):
        # Force polling mode by stubbing _try_inotify to return False.
        monkeypatch.setattr(fws, '_try_inotify', lambda *a, **k: False)
        # Run the polling loop fast.
        monkeypatch.setattr(fws, '_POLL_INTERVAL_SECONDS', 0.2)
        # Skip the "wait 60s for files to settle" guard so the file
        # appears immediately in the initial scan.
        monkeypatch.setattr(fws, '_MIN_FILE_AGE_SECONDS', 0)

        clip = tmp_path / "2025-11-08_08-15-44-front.mp4"
        clip.write_bytes(b'')

        deleted_paths = []
        deletion_event = threading.Event()

        def on_delete(paths):
            deleted_paths.extend(paths)
            deletion_event.set()

        fws.register_delete_callback(on_delete)
        assert fws.start_watcher([str(tmp_path)]) is True
        try:
            # Give the worker time to do its initial scan and add the file
            # to known_files.
            time.sleep(0.5)
            os.unlink(str(clip))
            # Wait for the next polling tick to surface the deletion.
            assert deletion_event.wait(timeout=3.0), \
                "delete callback never fired"
            assert any(p.endswith('-front.mp4') for p in deleted_paths)
        finally:
            fws.stop_watcher(timeout=3.0)


class TestArchiveCallback:
    """Phase 2a archive_queue producer (issue #76).

    The archive callback list is parallel to the existing mp4 callback
    list — both fire on the same ``_notify_callbacks`` invocation with
    the same paths. These tests exercise the wire-up; the producer's
    behavior (DB writes, priority inference) is covered separately in
    ``test_archive_queue.py`` and ``test_archive_producer.py``.
    """

    def test_register_archive_callback_appends_to_list(self):
        before = len(fws._on_archive_callbacks)
        fws.register_archive_callback(lambda paths: None)
        assert len(fws._on_archive_callbacks) == before + 1

    def test_archive_callback_fires_alongside_mp4_callback(self):
        mp4_received = []
        archive_received = []
        fws.register_callback(lambda paths: mp4_received.extend(paths))
        fws.register_archive_callback(
            lambda paths: archive_received.extend(paths)
        )
        current = fws._watcher_generation
        fws._notify_callbacks(['/foo/a.mp4', '/foo/b.mp4'],
                              my_generation=current)
        # Both subscribers see exactly the same list.
        assert mp4_received == ['/foo/a.mp4', '/foo/b.mp4']
        assert archive_received == ['/foo/a.mp4', '/foo/b.mp4']

    def test_archive_callback_dropped_when_generation_stale(self):
        # Same generation guard as the mp4 callbacks: a stale batch
        # must not fire archive callbacks either.
        archive_received = []
        fws.register_archive_callback(
            lambda paths: archive_received.extend(paths)
        )
        captured = fws._watcher_generation
        fws._watcher_generation = captured + 1  # simulate stop_watcher bump
        try:
            fws._notify_callbacks(['/x.mp4'], my_generation=captured)
        finally:
            fws._watcher_generation = captured
        assert archive_received == []

    def test_archive_callback_exception_does_not_block_others(self):
        # One bad archive subscriber can't starve a second one.
        good_received = []

        def bad_cb(paths):
            raise RuntimeError("synthetic bad subscriber")

        fws.register_archive_callback(bad_cb)
        fws.register_archive_callback(
            lambda paths: good_received.extend(paths)
        )
        current = fws._watcher_generation
        fws._notify_callbacks(['/y.mp4'], my_generation=current)
        assert good_received == ['/y.mp4']

    def test_no_archive_callback_when_none_registered(self):
        # Sanity: empty archive list, mp4 callback still fires.
        mp4_received = []
        fws.register_callback(lambda paths: mp4_received.extend(paths))
        current = fws._watcher_generation
        # Should not raise even though _on_archive_callbacks is empty.
        fws._notify_callbacks(['/z.mp4'], my_generation=current)
        assert mp4_received == ['/z.mp4']

    def test_mp4_callback_exception_does_not_block_archive(self):
        """The two callback lists are independent — one bad mp4
        subscriber must not prevent the archive callback from firing."""
        archive_received = []

        def bad_mp4(paths):
            raise RuntimeError("bad mp4 subscriber")

        fws.register_callback(bad_mp4)
        fws.register_archive_callback(
            lambda paths: archive_received.extend(paths)
        )
        current = fws._watcher_generation
        fws._notify_callbacks(['/q.mp4'], my_generation=current)
        assert archive_received == ['/q.mp4']
