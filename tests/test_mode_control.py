"""Tests for scripts/web/blueprints/mode_control.py.

Covers the worker pause/resume coordination introduced in PR #88
(Phase 2b of issue #76) and the asymmetric pause-failure cleanup
fixed in issue #89.
"""

import sys
import types
import unittest
from unittest.mock import MagicMock

# conftest.py already adds ``scripts/web`` to sys.path, but the real
# blueprint imports a long chain of services at module-load time.
# Import the blueprint once at module import; per-test we then swap
# only ``services.indexing_worker`` and ``services.archive_worker`` in
# sys.modules, which is what ``from services import indexing_worker``
# inside ``_pause_worker_for_mode_switch`` will resolve.
from blueprints import mode_control as mode_control_module  # noqa: E402


def _make_worker_module(*, is_running, pause_returns, raise_on_pause=None):
    """Build a stand-in for an indexing_worker / archive_worker module.

    Returns a SimpleNamespace exposing the three functions that
    ``_pause_worker_for_mode_switch`` interrogates (``is_running``,
    ``pause_worker``, ``resume_worker``) so tests can assert on call
    counts.
    """
    mod = types.SimpleNamespace()
    mod.is_running = MagicMock(return_value=is_running)
    if raise_on_pause is not None:
        mod.pause_worker = MagicMock(side_effect=raise_on_pause)
    else:
        mod.pause_worker = MagicMock(return_value=pause_returns)
    mod.resume_worker = MagicMock(return_value=None)
    return mod


class _SwapWorkers:
    """Context manager that temporarily installs stand-in worker modules.

    Restores whatever was previously in ``sys.modules`` for the two
    worker module paths so the rest of the test suite is unaffected.
    """

    def __init__(self, indexer, archive):
        self.indexer = indexer
        self.archive = archive
        self._prev_indexer = None
        self._prev_archive = None

    def __enter__(self):
        self._prev_indexer = sys.modules.get('services.indexing_worker')
        self._prev_archive = sys.modules.get('services.archive_worker')
        sys.modules['services.indexing_worker'] = self.indexer
        sys.modules['services.archive_worker'] = self.archive
        # Also bind on the parent package so ``from services import X``
        # resolves to our stand-in (it normally walks the package's
        # attribute table, not just sys.modules).
        import services  # noqa: WPS433
        self._prev_pkg_indexer = getattr(services, 'indexing_worker', None)
        self._prev_pkg_archive = getattr(services, 'archive_worker', None)
        services.indexing_worker = self.indexer
        services.archive_worker = self.archive
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._prev_indexer is not None:
            sys.modules['services.indexing_worker'] = self._prev_indexer
        else:
            sys.modules.pop('services.indexing_worker', None)
        if self._prev_archive is not None:
            sys.modules['services.archive_worker'] = self._prev_archive
        else:
            sys.modules.pop('services.archive_worker', None)
        import services  # noqa: WPS433
        if self._prev_pkg_indexer is not None:
            services.indexing_worker = self._prev_pkg_indexer
        else:
            try:
                delattr(services, 'indexing_worker')
            except AttributeError:
                pass
        if self._prev_pkg_archive is not None:
            services.archive_worker = self._prev_pkg_archive
        else:
            try:
                delattr(services, 'archive_worker')
            except AttributeError:
                pass


class TestPauseWorkerForModeSwitch(unittest.TestCase):
    """Regression coverage for issue #89.

    The bug: when one worker pauses successfully and the other fails,
    the function returned False (correctly refusing the mode switch)
    but the successfully-paused worker stayed paused indefinitely
    until either the next successful mode switch or a gadget_web
    restart. For the archive worker this was a bounded data-loss
    path because Tesla rotates RecentClips after ~60 minutes.
    """

    def test_both_pause_succeed_returns_true(self):
        indexer = _make_worker_module(is_running=True, pause_returns=True)
        archive = _make_worker_module(is_running=True, pause_returns=True)
        with _SwapWorkers(indexer, archive):
            self.assertTrue(
                mode_control_module._pause_worker_for_mode_switch()
            )
        indexer.pause_worker.assert_called_once()
        archive.pause_worker.assert_called_once()
        # Neither worker should be resumed by the pause helper itself —
        # the caller's try/finally is responsible for the post-switch
        # resume.
        indexer.resume_worker.assert_not_called()
        archive.resume_worker.assert_not_called()

    def test_indexer_pauses_archive_fails_unwinds_indexer(self):
        """Issue #89 (indexer-side): a successful indexer pause must
        be unwound when archive fails to pause.
        """
        indexer = _make_worker_module(is_running=True, pause_returns=True)
        archive = _make_worker_module(is_running=True, pause_returns=False)
        with _SwapWorkers(indexer, archive):
            self.assertFalse(
                mode_control_module._pause_worker_for_mode_switch()
            )
        # Indexer was paused → must be unwound exactly once via the
        # centralized cleanup.
        indexer.resume_worker.assert_called_once()
        # Archive's own pause returned False → its inner self-recovery
        # block already called resume_worker once. The centralized
        # unwind does NOT fire on archive because archive_paused is
        # False.
        self.assertEqual(archive.resume_worker.call_count, 1)

    def test_archive_pauses_indexer_fails_unwinds_archive(self):
        """Issue #89 (archive-side): a successful archive pause must
        be unwound when indexer fails to pause.
        """
        indexer = _make_worker_module(is_running=True, pause_returns=False)
        archive = _make_worker_module(is_running=True, pause_returns=True)
        with _SwapWorkers(indexer, archive):
            self.assertFalse(
                mode_control_module._pause_worker_for_mode_switch()
            )
        self.assertEqual(indexer.resume_worker.call_count, 1)
        archive.resume_worker.assert_called_once()

    def test_archive_pause_raises_after_indexer_paused(self):
        """An exception during archive pause must still unwind a
        paused indexer.
        """
        indexer = _make_worker_module(is_running=True, pause_returns=True)
        archive = _make_worker_module(
            is_running=True,
            pause_returns=None,
            raise_on_pause=RuntimeError("synthetic pause failure"),
        )
        with _SwapWorkers(indexer, archive):
            self.assertFalse(
                mode_control_module._pause_worker_for_mode_switch()
            )
        indexer.resume_worker.assert_called_once()

    def test_indexer_pause_raises_with_archive_paused(self):
        """An exception during indexer pause (with mapping enabled)
        must still unwind a paused archive worker.
        """
        indexer = _make_worker_module(
            is_running=True,
            pause_returns=None,
            raise_on_pause=RuntimeError("synthetic pause failure"),
        )
        archive = _make_worker_module(is_running=True, pause_returns=True)

        import config as config_module
        original_mapping = getattr(config_module, 'MAPPING_ENABLED', None)
        config_module.MAPPING_ENABLED = True
        try:
            with _SwapWorkers(indexer, archive):
                self.assertFalse(
                    mode_control_module._pause_worker_for_mode_switch()
                )
            archive.resume_worker.assert_called_once()
        finally:
            if original_mapping is None:
                try:
                    delattr(config_module, 'MAPPING_ENABLED')
                except AttributeError:
                    pass
            else:
                config_module.MAPPING_ENABLED = original_mapping

    def test_neither_worker_running_returns_true(self):
        indexer = _make_worker_module(is_running=False, pause_returns=True)
        archive = _make_worker_module(is_running=False, pause_returns=True)
        with _SwapWorkers(indexer, archive):
            self.assertTrue(
                mode_control_module._pause_worker_for_mode_switch()
            )
        indexer.pause_worker.assert_not_called()
        archive.pause_worker.assert_not_called()
        indexer.resume_worker.assert_not_called()
        archive.resume_worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
