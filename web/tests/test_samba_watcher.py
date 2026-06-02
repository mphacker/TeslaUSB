from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
from teslausb_web.config import PathsSection, SambaSection, SambaShareConfig, WebConfig, WebSection
from teslausb_web.services.samba_watcher import (
    SambaWatcher,
    SambaWatcherConfig,
    SambaWatcherError,
    make_samba_watcher,
)


class RecordingInvalidator:
    def __init__(self) -> None:
        self.calls = 0
        self.timestamps: list[float] = []
        self.event = threading.Event()
        self.lock = threading.Lock()

    def invalidate_now(self) -> object:
        with self.lock:
            self.calls += 1
            self.timestamps.append(time.monotonic())
            self.event.set()
        return {"calls": self.calls}


def _wait_until(
    predicate: Callable[[], bool],
    timeout: float = 2.0,
    interval: float = 0.02,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def watch_root(tmp_path: Path) -> Path:
    root = tmp_path / "TeslaCam"
    root.mkdir()
    return root


@pytest.fixture
def invalidator() -> RecordingInvalidator:
    return RecordingInvalidator()


@pytest.fixture
def watcher(watch_root: Path, invalidator: RecordingInvalidator) -> SambaWatcher:
    return SambaWatcher(
        SambaWatcherConfig(
            watched_paths=(watch_root,),
            invalidate_debounce_ms=80,
            watcher_poll_interval_seconds=0.02,
        ),
        invalidator,
    )


class TestSambaWatcherConfig:
    def test_rejects_relative_watch_path(self) -> None:
        with pytest.raises(SambaWatcherError, match="watched_paths"):
            SambaWatcherConfig(watched_paths=(Path("relative"),))

    def test_rejects_negative_debounce(self, watch_root: Path) -> None:
        with pytest.raises(SambaWatcherError, match="invalidate_debounce_ms"):
            SambaWatcherConfig(watched_paths=(watch_root,), invalidate_debounce_ms=-1)

    def test_rejects_nonpositive_poll_interval(self, watch_root: Path) -> None:
        with pytest.raises(SambaWatcherError, match="watcher_poll_interval_seconds"):
            SambaWatcherConfig(watched_paths=(watch_root,), watcher_poll_interval_seconds=0.0)

    def test_rejects_bad_ignore_extension(self, watch_root: Path) -> None:
        with pytest.raises(SambaWatcherError, match="start with"):
            SambaWatcherConfig(watched_paths=(watch_root,), ignore_extensions=("tmp",))


class TestSambaWatcherLifecycle:
    def test_start_stop_idempotent(self, watcher: SambaWatcher) -> None:
        assert watcher.start() is True
        assert watcher.start() is False
        assert watcher.shutdown(timeout=1.0) is True
        assert watcher.shutdown(timeout=1.0) is True

    def test_status_reports_running_state(self, watcher: SambaWatcher) -> None:
        watcher.start()
        assert _wait_until(lambda: watcher.status().running)
        assert watcher.shutdown(timeout=1.0) is True

    def test_status_reports_stopped_state(self, watcher: SambaWatcher) -> None:
        watcher.start()
        watcher.shutdown(timeout=1.0)
        assert watcher.status().running is False

    def test_shutdown_without_start_returns_true(self, watcher: SambaWatcher) -> None:
        assert watcher.shutdown(timeout=0.1) is True

    def test_shutdown_returns_within_budget(self, watcher: SambaWatcher) -> None:
        watcher.start()
        started = time.monotonic()
        stopped = watcher.shutdown(timeout=0.5)
        elapsed = time.monotonic() - started
        assert stopped is True
        assert elapsed < 0.5

    def test_thread_joins_cleanly(self, watcher: SambaWatcher) -> None:
        watcher.start()
        assert watcher.shutdown(timeout=1.0) is True
        assert watcher._thread is None

    def test_missing_watched_path_is_tolerated(
        self, tmp_path: Path, invalidator: RecordingInvalidator
    ) -> None:
        watcher = SambaWatcher(
            SambaWatcherConfig(
                watched_paths=(tmp_path / "missing",),
                invalidate_debounce_ms=20,
                watcher_poll_interval_seconds=0.02,
            ),
            invalidator,
        )
        assert watcher.start() is True
        assert watcher.shutdown(timeout=1.0) is True

    def test_factory_uses_configured_values(
        self, tmp_path: Path, invalidator: RecordingInvalidator
    ) -> None:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=tmp_path / "backing",
                state_dir=tmp_path / "state",
                ipc_socket=tmp_path / "ipc" / "worker.sock",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
            samba=SambaSection(
                shares=(SambaShareConfig(name="Media", path=tmp_path / "backing" / "Media"),),
                invalidate_debounce_ms=25,
                watcher_poll_interval_seconds=0.05,
                ignore_extensions=(".tmp", ".part"),
                ignore_dotfiles=False,
            ),
        )
        built = make_samba_watcher(cfg, invalidator)
        assert built.config.watched_paths == (tmp_path / "backing" / "Media",)
        assert built.config.invalidate_debounce_ms == 25
        assert built.config.ignore_dotfiles is False

    def test_factory_falls_back_to_backing_root_when_no_shares(
        self,
        tmp_path: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=tmp_path / "backing",
                state_dir=tmp_path / "state",
                ipc_socket=tmp_path / "ipc" / "worker.sock",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
        )
        built = make_samba_watcher(cfg, invalidator)
        # media_root defaults to backing_root, which is an ancestor of
        # backing/TeslaCam — so we watch only the ancestor (covers both).
        assert built.config.watched_paths == (tmp_path / "backing",)

    def test_factory_watches_only_media_when_media_root_disjoint(
        self,
        tmp_path: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        cfg = WebConfig(
            web=WebSection(secret_key="x" * 32),
            paths=PathsSection(
                backing_root=tmp_path / "backing",
                media_root=tmp_path / "media",
                state_dir=tmp_path / "state",
                ipc_socket=tmp_path / "ipc" / "worker.sock",
                cache_invalidate_script=tmp_path / "invalidate.sh",
            ),
        )
        built = make_samba_watcher(cfg, invalidator)
        # The TeslaCam dashcam tree is deliberately NOT watched: Tesla
        # churns it continuously, which would peg a CPU core and fire
        # spurious invalidations. Only the media tree (lock chimes,
        # light shows, boombox, music) is watched.
        assert built.config.watched_paths == (tmp_path / "media",)


class TestSambaWatcherEvents:
    def test_debounce_coalesces_multiple_changes(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        file_path = watch_root / "clip.mp4"
        watcher.start()
        file_path.write_text("a", encoding="utf-8")
        assert _wait_until(lambda: watcher.status().events_since_start >= 1)
        file_path.write_text("b", encoding="utf-8")
        file_path.write_text("c", encoding="utf-8")
        assert _wait_until(lambda: invalidator.calls == 1)
        assert watcher.shutdown(timeout=1.0) is True

    def test_allowed_extension_triggers_invalidation(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        watcher.start()
        (watch_root / "movie.mp4").write_text("x", encoding="utf-8")
        assert _wait_until(lambda: invalidator.calls == 1)
        watcher.shutdown(timeout=1.0)

    def test_ignored_extension_does_not_trigger(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        watcher.start()
        (watch_root / "upload.tmp").write_text("x", encoding="utf-8")
        time.sleep(0.25)
        watcher.shutdown(timeout=1.0)
        assert invalidator.calls == 0

    def test_ignored_dotfile_does_not_trigger(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        watcher.start()
        (watch_root / ".hidden.mp4").write_text("x", encoding="utf-8")
        time.sleep(0.25)
        watcher.shutdown(timeout=1.0)
        assert invalidator.calls == 0

    def test_ignore_dotfiles_can_be_disabled(
        self, watch_root: Path, invalidator: RecordingInvalidator
    ) -> None:
        watcher = SambaWatcher(
            SambaWatcherConfig(
                watched_paths=(watch_root,),
                invalidate_debounce_ms=20,
                watcher_poll_interval_seconds=0.02,
                ignore_dotfiles=False,
            ),
            invalidator,
        )
        watcher.start()
        (watch_root / ".visible.mp4").write_text("x", encoding="utf-8")
        assert _wait_until(lambda: invalidator.calls == 1)
        watcher.shutdown(timeout=1.0)

    def test_recursive_subdirectory_changes_trigger(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        nested = watch_root / "SavedClips" / "2026-01-01"
        nested.mkdir(parents=True)
        watcher.start()
        (nested / "clip.mp4").write_text("x", encoding="utf-8")
        assert _wait_until(lambda: invalidator.calls == 1)
        watcher.shutdown(timeout=1.0)

    def test_dot_directory_is_ignored(
        self, watch_root: Path, invalidator: RecordingInvalidator
    ) -> None:
        watcher = SambaWatcher(
            SambaWatcherConfig(
                watched_paths=(watch_root,),
                invalidate_debounce_ms=20,
                watcher_poll_interval_seconds=0.02,
            ),
            invalidator,
        )
        ignored_dir = watch_root / ".staging"
        ignored_dir.mkdir()
        watcher.start()
        (ignored_dir / "clip.mp4").write_text("x", encoding="utf-8")
        time.sleep(0.25)
        watcher.shutdown(timeout=1.0)
        assert invalidator.calls == 0

    def test_delete_triggers_invalidation(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        file_path = watch_root / "clip.mp4"
        watcher.start()
        file_path.write_text("x", encoding="utf-8")
        assert _wait_until(lambda: invalidator.calls == 1)
        file_path.unlink()
        assert _wait_until(lambda: invalidator.calls == 2)
        watcher.shutdown(timeout=1.0)

    def test_shutdown_drains_pending_invalidation(
        self,
        watch_root: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        watcher = SambaWatcher(
            SambaWatcherConfig(
                watched_paths=(watch_root,),
                invalidate_debounce_ms=500,
                watcher_poll_interval_seconds=0.02,
            ),
            invalidator,
        )
        watcher.start()
        (watch_root / "clip.mp4").write_text("x", encoding="utf-8")
        assert _wait_until(lambda: watcher.status().events_since_start >= 1)
        assert watcher.shutdown(timeout=1.0) is True
        assert invalidator.calls == 1

    def test_status_tracks_event_count(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
    ) -> None:
        watcher.start()
        (watch_root / "clip.mp4").write_text("x", encoding="utf-8")
        assert _wait_until(lambda: watcher.status().events_since_start >= 1)
        watcher.shutdown(timeout=1.0)
        assert watcher.status().events_since_start >= 1

    def test_status_tracks_last_event_timestamp(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
    ) -> None:
        watcher.start()
        (watch_root / "clip.mp4").write_text("x", encoding="utf-8")
        assert _wait_until(lambda: watcher.status().last_event_at is not None)
        watcher.shutdown(timeout=1.0)

    def test_multiple_burst_events_still_single_invalidation(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        watcher.start()
        for index in range(5):
            (watch_root / f"clip-{index}.mp4").write_text(str(index), encoding="utf-8")
        assert _wait_until(lambda: invalidator.calls == 1)
        watcher.shutdown(timeout=1.0)

    def test_windows_runner_fallback_works(
        self, tmp_path: Path, invalidator: RecordingInvalidator
    ) -> None:
        watched = tmp_path / "watched"
        watched.mkdir()
        watcher = SambaWatcher(
            SambaWatcherConfig(
                watched_paths=(watched,),
                invalidate_debounce_ms=10,
                watcher_poll_interval_seconds=0.02,
            ),
            invalidator,
        )
        watcher.start()
        (watched / "clip.mp4").write_text("x", encoding="utf-8")
        assert _wait_until(lambda: invalidator.calls == 1)
        watcher.shutdown(timeout=1.0)

    def test_status_exposes_watched_paths(self, watcher: SambaWatcher, watch_root: Path) -> None:
        assert watcher.status().watched_paths == (watch_root,)

    def test_ignored_extension_is_case_insensitive(
        self,
        watcher: SambaWatcher,
        watch_root: Path,
        invalidator: RecordingInvalidator,
    ) -> None:
        watcher.start()
        (watch_root / "upload.TMP").write_text("x", encoding="utf-8")
        time.sleep(0.25)
        watcher.shutdown(timeout=1.0)
        assert invalidator.calls == 0
