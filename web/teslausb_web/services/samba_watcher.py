"""Polling-based Samba watcher.

B-1 intentionally avoids adding a new inotify dependency here. Python's
stdlib has no cross-platform inotify wrapper, so the watcher uses recursive
polling via ``os.scandir``. The trade-off is bounded detection latency equal
to ``watcher_poll_interval_seconds``; the benefit is that the exact same code
runs on Windows CI and Linux targets.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_DEFAULT_DEBOUNCE_MS: int = 500
_DEFAULT_POLL_INTERVAL_SECONDS: float = 1.0
_DEFAULT_IGNORE_EXTENSIONS: tuple[str, ...] = (".tmp", ".part", ".swp", ".crdownload")
_DEFAULT_WATCH_NAME: str = "TeslaCam"


class CacheInvalidatorLike(Protocol):
    def invalidate_now(self) -> object: ...


class SambaWatcherError(RuntimeError):
    """Raised when the Samba watcher is misconfigured or cannot start."""


@dataclass(frozen=True, slots=True)
class WatcherStatus:
    running: bool
    watched_paths: tuple[Path, ...]
    events_since_start: int
    last_event_at: float | None


@dataclass(frozen=True, slots=True)
class SambaWatcherConfig:
    watched_paths: tuple[Path, ...]
    invalidate_debounce_ms: int = _DEFAULT_DEBOUNCE_MS
    watcher_poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS
    ignore_extensions: tuple[str, ...] = _DEFAULT_IGNORE_EXTENSIONS
    ignore_dotfiles: bool = True

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for watched_path in self.watched_paths:
            if (
                not watched_path.is_absolute()
                and not PurePosixPath(watched_path.as_posix()).is_absolute()
            ):
                raise SambaWatcherError(f"watched_paths must be absolute, got {watched_path!r}")
        if self.invalidate_debounce_ms < 0:
            raise SambaWatcherError("invalidate_debounce_ms must be >= 0")
        if self.watcher_poll_interval_seconds <= 0:
            raise SambaWatcherError("watcher_poll_interval_seconds must be > 0")
        for extension in self.ignore_extensions:
            if not extension.strip():
                raise SambaWatcherError("ignore_extensions entries must be non-empty")
            if not extension.startswith("."):
                raise SambaWatcherError("ignore_extensions entries must start with '.'")


@dataclass(frozen=True, slots=True)
class _EntryState:
    is_dir: bool
    mtime_ns: int
    size_bytes: int


class SambaWatcher:
    """Recursive polling watcher that debounces cache invalidations."""

    def __init__(
        self,
        config: SambaWatcherConfig,
        cache_invalidator: CacheInvalidatorLike,
        *,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        config.validate()
        self._config = config
        self._cache_invalidator = cache_invalidator
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._started_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._events_since_start = 0
        self._last_event_at: float | None = None
        self._running = False

    @property
    def config(self) -> SambaWatcherConfig:
        return self._config

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._started_event.clear()
            thread = threading.Thread(target=self._worker_loop, name="samba-watcher", daemon=True)
            self._thread = thread
            self._running = True
        thread.start()
        self._started_event.wait(timeout=self._config.watcher_poll_interval_seconds)
        return True

    def shutdown(self, timeout: float = 5.0) -> bool:
        if timeout < 0:
            raise SambaWatcherError("timeout must be >= 0")
        with self._lock:
            thread = self._thread
            if thread is None:
                self._running = False
                return True
            self._stop_event.set()
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
                    self._running = False
        return stopped

    def status(self) -> WatcherStatus:
        with self._lock:
            thread = self._thread
            running = self._running and thread is not None and thread.is_alive()
            return WatcherStatus(
                running=running,
                watched_paths=self._config.watched_paths,
                events_since_start=self._events_since_start,
                last_event_at=self._last_event_at,
            )

    def _worker_loop(self) -> None:
        previous_snapshot = self._build_snapshot()
        self._started_event.set()
        pending_invalidation = False
        pending_deadline: float | None = None
        try:
            while True:
                if self._stop_event.is_set():
                    if pending_invalidation:
                        self._invalidate_now()
                    break
                current_snapshot = self._build_snapshot()
                change_count = self._diff_count(previous_snapshot, current_snapshot)
                if change_count > 0:
                    now = self._monotonic()
                    with self._lock:
                        self._events_since_start += change_count
                        self._last_event_at = now
                    pending_invalidation = True
                    pending_deadline = now + (self._config.invalidate_debounce_ms / 1000.0)
                previous_snapshot = current_snapshot
                if pending_invalidation and pending_deadline is not None:
                    now = self._monotonic()
                    if now >= pending_deadline:
                        self._invalidate_now()
                        pending_invalidation = False
                        pending_deadline = None
                        continue
                    timeout = min(
                        self._config.watcher_poll_interval_seconds,
                        max(0.0, pending_deadline - now),
                    )
                else:
                    timeout = self._config.watcher_poll_interval_seconds
                self._stop_event.wait(timeout)
        finally:
            with self._lock:
                self._running = False
                if self._thread is threading.current_thread():
                    self._thread = None

    def _invalidate_now(self) -> None:
        logger.info("Samba watcher invalidating Tesla cache")
        self._cache_invalidator.invalidate_now()

    def _build_snapshot(self) -> dict[Path, _EntryState]:
        snapshot: dict[Path, _EntryState] = {}
        for watched_path in self._config.watched_paths:
            self._scan_path(watched_path.resolve(strict=False), snapshot)
        return snapshot

    def _scan_path(self, root: Path, snapshot: dict[Path, _EntryState]) -> None:
        if self._should_ignore_name(root.name):
            return
        try:
            stat_result = root.stat()
        except OSError:
            return
        if not root.is_dir():
            snapshot[root] = _EntryState(
                is_dir=False,
                mtime_ns=stat_result.st_mtime_ns,
                size_bytes=int(stat_result.st_size),
            )
            return
        try:
            with os.scandir(root) as iterator:
                for entry in iterator:
                    entry_path = Path(entry.path)
                    if self._should_ignore_name(entry.name):
                        continue
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if is_dir:
                        self._scan_path(entry_path, snapshot)
                        continue
                    snapshot[entry_path] = _EntryState(
                        is_dir=False,
                        mtime_ns=entry_stat.st_mtime_ns,
                        size_bytes=int(entry_stat.st_size),
                    )
        except OSError:
            logger.debug("Samba watcher could not scan %s", root, exc_info=True)

    def _should_ignore_name(self, name: str) -> bool:
        if self._config.ignore_dotfiles and name.startswith("."):
            return True
        lower_name = name.lower()
        return any(
            lower_name.endswith(extension.lower()) for extension in self._config.ignore_extensions
        )

    def _diff_count(
        self,
        previous: dict[Path, _EntryState],
        current: dict[Path, _EntryState],
    ) -> int:
        changed = 0
        all_paths = set(previous) | set(current)
        for path in all_paths:
            if previous.get(path) != current.get(path):
                changed += 1
        return changed


def _watched_paths(cfg: WebConfig) -> tuple[Path, ...]:
    if cfg.samba.shares:
        return tuple(share.path for share in cfg.samba.shares)
    # Mirror samba_service._default_shares: when no explicit shares are
    # configured, both default LUNs are exposed, so both must be watched.
    # If media_root is an ancestor of (or equal to) teslacam_path —
    # which happens in tests that stage a single backing tree — watch
    # only media_root because it already covers TeslaCam recursively.
    media_root = cfg.paths.media_root or cfg.paths.backing_root
    teslacam_path = cfg.paths.backing_root / _DEFAULT_WATCH_NAME
    try:
        teslacam_path.relative_to(media_root)
    except ValueError:
        # teslacam_path is NOT under media_root — they're disjoint trees.
        return (teslacam_path, media_root)
    return (teslacam_path,) if media_root == teslacam_path else (media_root,)


def make_samba_watcher(cfg: WebConfig, cache_invalidator: CacheInvalidatorLike) -> SambaWatcher:
    return SambaWatcher(
        SambaWatcherConfig(
            watched_paths=_watched_paths(cfg),
            invalidate_debounce_ms=cfg.samba.invalidate_debounce_ms,
            watcher_poll_interval_seconds=cfg.samba.watcher_poll_interval_seconds,
            ignore_extensions=cfg.samba.ignore_extensions,
            ignore_dotfiles=cfg.samba.ignore_dotfiles,
        ),
        cache_invalidator,
    )


__all__ = (
    "CacheInvalidatorLike",
    "SambaWatcher",
    "SambaWatcherConfig",
    "SambaWatcherError",
    "WatcherStatus",
    "make_samba_watcher",
)
