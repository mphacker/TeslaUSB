"""Worker controller and mutable state for cloud archive."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from teslausb_web.services.cloud_archive.queue_ops import (
    recover_interrupted_uploads as recover_queue,
)
from teslausb_web.services.cloud_archive.settings import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
from teslausb_web.services.cloud_archive.uploader import _run_sync
from teslausb_web.services.cloud_archive.wifi import _is_wifi_connected

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from teslausb_web.services.cloud_archive.discovery import EventCandidate
    from teslausb_web.services.cloud_archive.service import CloudArchiveService


@dataclass(frozen=True, slots=True)
class SyncStatus:
    running: bool
    progress: str
    files_total: int
    files_done: int
    bytes_transferred: int
    total_bytes: int
    current_file: str
    current_file_size: int
    started_at: float | None
    last_run: str | None
    error: str | None
    worker_running: bool
    wake_count: int
    drain_count: int
    eta_seconds: int | None
    throughput_bps: int | None


@dataclass(slots=True)
class WorkerState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    wake_event: threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    startup_recovery_done: bool = False
    running: bool = False
    progress: str = ""
    files_total: int = 0
    files_done: int = 0
    bytes_transferred: int = 0
    total_bytes: int = 0
    current_file: str = ""
    current_file_size: int = 0
    started_at: float | None = None
    last_run: str | None = None
    error: str | None = None
    worker_running: bool = False
    wake_count: int = 0
    drain_count: int = 0
    shadow_agreement_count: int = 0
    shadow_disagreement_count: int = 0
    pipeline_enqueue_count: int = 0

    def snapshot(self) -> SyncStatus:
        with self.lock:
            throughput = None
            eta = None
            if self.running and self.started_at is not None and self.bytes_transferred > 0:
                elapsed = max(1e-6, time.monotonic() - self.started_at)
                throughput = int(self.bytes_transferred / elapsed)
                remaining = max(0, self.total_bytes - self.bytes_transferred)
                eta = int(remaining / throughput) if throughput > 0 else None
            return SyncStatus(
                running=self.running,
                progress=self.progress,
                files_total=self.files_total,
                files_done=self.files_done,
                bytes_transferred=self.bytes_transferred,
                total_bytes=self.total_bytes,
                current_file=self.current_file,
                current_file_size=self.current_file_size,
                started_at=self.started_at,
                last_run=self.last_run,
                error=self.error,
                worker_running=self.worker_running,
                wake_count=self.wake_count,
                drain_count=self.drain_count,
                eta_seconds=eta,
                throughput_bps=throughput,
            )

    def begin_drain(self, trigger: str) -> None:
        with self.lock:
            self.running = True
            self.progress = f"Scanning for events ({trigger})"
            self.files_total = 0
            self.files_done = 0
            self.bytes_transferred = 0
            self.total_bytes = 0
            self.current_file = ""
            self.current_file_size = 0
            self.started_at = time.monotonic()
            self.error = None

    def set_totals(self, candidates: tuple[EventCandidate, ...]) -> None:
        with self.lock:
            self.files_total = len(candidates)
            self.total_bytes = sum(candidate.size_bytes for candidate in candidates)
            self.progress = f"Syncing {len(candidates)} item(s)"

    def set_current(self, candidate: EventCandidate) -> None:
        with self.lock:
            self.current_file = candidate.relative_path
            self.current_file_size = candidate.size_bytes
            self.progress = f"Uploading {candidate.relative_path}"

    def record_success(self, transferred_bytes: int) -> None:
        with self.lock:
            self.files_done += 1
            self.bytes_transferred += transferred_bytes

    def record_failure(self, error_message: str) -> None:
        with self.lock:
            self.error = error_message
            self.files_done += 1

    def finish_drain(self) -> None:
        with self.lock:
            self.running = False
            self.current_file = ""
            self.current_file_size = 0
            self.last_run = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.progress = "Idle"

    def note_shadow_agreement(self) -> None:
        with self.lock:
            self.shadow_agreement_count += 1

    def note_shadow_disagreement(self) -> None:
        with self.lock:
            self.shadow_disagreement_count += 1

    def note_pipeline_enqueue(self, count: int) -> None:
        with self.lock:
            self.pipeline_enqueue_count += count


class CloudArchiveWorker:
    def __init__(
        self,
        service: CloudArchiveService,
        wifi_checker: Callable[[], bool] = _is_wifi_connected,
    ) -> None:
        self._service = service
        self._wifi_checker = wifi_checker

    def _worker_loop(self) -> None:
        state = self._service.state
        with state.lock:
            state.worker_running = True
        try:
            if not state.startup_recovery_done:
                self.recover_interrupted_uploads()
                state.startup_recovery_done = True
            state.wake_event.set()
            while not state.stop_event.is_set():
                state.wake_event.wait(timeout=self._service.config.worker_idle_seconds)
                state.wake_event.clear()
                if state.stop_event.is_set():
                    break
                with state.lock:
                    state.wake_count += 1
                if not self._service.is_auto_sync_enabled():
                    continue
                if self._service.oauth_service.load_credentials() is None:
                    continue
                if self._service.config.wifi_check_required and not self._wifi_checker():
                    state.stop_event.wait(timeout=self._service.config.backoff_initial_seconds)
                    continue
                with state.lock:
                    state.drain_count += 1
                try:
                    _run_sync(self._service, "auto")
                    try:
                        from teslausb_web.services.cloud_archive.cloud_cleanup import (
                            run_cloud_cleanup,
                        )

                        run_cloud_cleanup(self._service)
                    except Exception:  # pragma: no cover - defensive, never fail loop
                        logger.exception("cloud cleanup post-drain raised")
                finally:
                    state.cancel_event.clear()
        finally:
            with state.lock:
                state.worker_running = False

    def start(self) -> bool:
        state = self._service.state
        with state.lock:
            if state.thread is not None and state.thread.is_alive():
                return False
            state.stop_event.clear()
            state.cancel_event.clear()
            state.wake_event.clear()
            thread = threading.Thread(
                target=self._worker_loop,
                name="cloud-archive-worker",
                daemon=True,
            )
            state.thread = thread
        thread.start()
        return True

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        state = self._service.state
        state.stop_event.set()
        state.cancel_event.set()
        state.wake_event.set()
        self._service.rclone_service.cancel_active_transfer()
        if state.thread is None:
            return True
        state.thread.join(timeout=timeout)
        return not state.thread.is_alive()

    def wake(self) -> None:
        self._service.state.wake_event.set()

    def start_sync(self, trigger: str = "manual") -> tuple[bool, str]:
        if self._service.oauth_service.load_credentials() is None:
            return False, "No cloud provider configured"
        self.start()
        self.wake()
        return True, f"Cloud sync wake delivered ({trigger})"

    def stop_sync(self) -> tuple[bool, str]:
        state = self._service.state
        if not state.running:
            return False, "Sync is not running"
        state.cancel_event.set()
        state.wake_event.set()
        self._service.rclone_service.cancel_active_transfer()
        return True, "Sync stopping"

    def trigger_auto_sync(self) -> None:
        self.start()
        self.wake()

    def recover_interrupted_uploads(self) -> int:
        return recover_queue(self._service.config)

    def get_sync_status(self) -> SyncStatus:
        return self._service.state.snapshot()
