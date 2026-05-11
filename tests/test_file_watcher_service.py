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
        # Phase 2b: indexing callbacks only fire for paths under
        # ARCHIVE_DIR. Use a real ArchivedClips-prefixed path so the
        # classifier routes it correctly; otherwise it gets dropped.
        prefix = fws._archive_dir_prefix() or '/tmp'
        path = os.path.join(prefix, 'a', 'b.mp4')
        fws._notify_callbacks([path], my_generation=current)
        assert received == [path]

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
    """Phase 2a archive_queue producer + Phase 2b path-classified routing
    (issue #76).

    Phase 2a wired the archive callback list parallel to the existing
    mp4 callback list. Phase 2b changed ``_notify_callbacks`` to
    classify paths and route them to ONE list or the OTHER (never
    both): paths under the RO USB mount fire only archive callbacks;
    paths under ``ARCHIVE_DIR`` fire only indexing callbacks.

    These tests pin both behaviors. Paths use ``_ro_mount_prefixes`` /
    ``_archive_dir_prefix`` rather than hardcoded strings so the tests
    survive future changes to the default mount layout.
    """

    @staticmethod
    def _ro_path(name: str) -> str:
        prefixes = fws._ro_mount_prefixes()
        # Use the first candidate (``<MNT_DIR>/part1-ro``); both are
        # valid but the first matches what production uses.
        return os.path.join(prefixes[0], 'TeslaCam', 'RecentClips', name)

    @staticmethod
    def _archive_path(name: str) -> str:
        prefix = fws._archive_dir_prefix()
        if not prefix:
            pytest.skip("ARCHIVE_DIR not configured in this environment")
        return os.path.join(prefix, 'RecentClips', name)

    def test_register_archive_callback_appends_to_list(self):
        before = len(fws._on_archive_callbacks)
        fws.register_archive_callback(lambda paths: None)
        assert len(fws._on_archive_callbacks) == before + 1

    def test_archive_callback_fires_alongside_mp4_callback(self):
        # Phase 2b: a single batch with one RO-mount path AND one
        # ArchivedClips path must split — RO fires archive only,
        # ArchivedClips fires indexing only. The two callback lists
        # see disjoint subsets of the original batch.
        mp4_received = []
        archive_received = []
        fws.register_callback(lambda paths: mp4_received.extend(paths))
        fws.register_archive_callback(
            lambda paths: archive_received.extend(paths)
        )
        ro_a = self._ro_path('a.mp4')
        ro_b = self._ro_path('b.mp4')
        arch_c = self._archive_path('c.mp4')
        current = fws._watcher_generation
        fws._notify_callbacks([ro_a, ro_b, arch_c],
                              my_generation=current)
        # RO-mount paths route to archive only.
        assert archive_received == [ro_a, ro_b]
        # ArchivedClips path routes to indexing only.
        assert mp4_received == [arch_c]

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
            fws._notify_callbacks([self._ro_path('x.mp4')],
                                  my_generation=captured)
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
        ro_y = self._ro_path('y.mp4')
        current = fws._watcher_generation
        fws._notify_callbacks([ro_y], my_generation=current)
        assert good_received == [ro_y]

    def test_no_archive_callback_when_none_registered(self):
        # Sanity: empty archive list, mp4 callback still fires for
        # ArchivedClips paths (RO-mount paths would just be silently
        # routed to the empty archive list — also fine).
        mp4_received = []
        fws.register_callback(lambda paths: mp4_received.extend(paths))
        arch_z = self._archive_path('z.mp4')
        current = fws._watcher_generation
        # Should not raise even though _on_archive_callbacks is empty.
        fws._notify_callbacks([arch_z], my_generation=current)
        assert mp4_received == [arch_z]

    def test_mp4_callback_exception_does_not_block_archive(self):
        """The two callback lists are independent — one bad mp4
        subscriber must not prevent the archive callback from firing.

        Phase 2b: the bad mp4 subscriber receives ArchivedClips paths;
        the archive callback receives RO-mount paths. They no longer
        overlap on the same path, so this test sends one of each and
        verifies BOTH still fire even when the mp4 side raises."""
        archive_received = []

        def bad_mp4(paths):
            raise RuntimeError("bad mp4 subscriber")

        fws.register_callback(bad_mp4)
        fws.register_archive_callback(
            lambda paths: archive_received.extend(paths)
        )
        ro_q = self._ro_path('q.mp4')
        arch_q = self._archive_path('q.mp4')
        current = fws._watcher_generation
        fws._notify_callbacks([ro_q, arch_q], my_generation=current)
        assert archive_received == [ro_q]


class TestPathClassification:
    """Phase 2b routing rule pinned by direct unit tests on
    :func:`_classify_paths` (issue #76)."""

    def test_ro_mount_path_routes_to_archive(self):
        ro = os.path.join(fws._ro_mount_prefixes()[0], 'TeslaCam', 'foo.mp4')
        archive, indexing, dropped = fws._classify_paths([ro])
        assert archive == [ro]
        assert indexing == []
        assert dropped == []

    def test_archive_dir_path_routes_to_indexing(self):
        prefix = fws._archive_dir_prefix()
        if not prefix:
            pytest.skip("ARCHIVE_DIR not configured")
        arch = os.path.join(prefix, 'RecentClips', 'bar.mp4')
        archive, indexing, dropped = fws._classify_paths([arch])
        assert archive == []
        assert indexing == [arch]
        assert dropped == []

    def test_unrelated_path_dropped(self):
        # /tmp/foo.mp4 belongs to neither prefix.
        archive, indexing, dropped = fws._classify_paths(
            ['/random/scratch/foo.mp4'],
        )
        assert archive == []
        assert indexing == []
        assert dropped == ['/random/scratch/foo.mp4']

    def test_empty_string_skipped(self):
        archive, indexing, dropped = fws._classify_paths(['', None])
        assert archive == []
        assert indexing == []
        assert dropped == []

    def test_archive_prefix_wins_when_both_match(self, monkeypatch):
        # Defensive: if someone misconfigures ARCHIVE_DIR to live
        # under the RO mount (it shouldn't, but…), the archive prefix
        # check runs first and routes to indexing. This stops a
        # double-enqueue that would otherwise re-archive a file the
        # worker just wrote.
        ro_root = fws._ro_mount_prefixes()[0]
        weird = os.path.join(ro_root, 'ArchivedClips')
        monkeypatch.setattr(fws, '_archive_dir_prefix', lambda: weird)
        path = os.path.join(weird, 'RecentClips', 'q.mp4')
        archive, indexing, dropped = fws._classify_paths([path])
        assert archive == []
        assert indexing == [path]
        assert dropped == []
