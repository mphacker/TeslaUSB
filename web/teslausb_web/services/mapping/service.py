from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from numbers import Real
from pathlib import Path
from typing import TYPE_CHECKING

from teslausb_web.services.mapping_migrations import (
    MigrationsConfig,
    MigrationsRunner,
    make_migrations_runner,
)
from teslausb_web.services.mapping_queries import MappingQueries, MappingQueriesConfig, Stats

from .kv import _kv_get, _kv_set

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from teslausb_web.config import WebConfig

    from .sei import SeiParserProtocol

logger = logging.getLogger(__name__)

_DEFAULT_ARCHIVED_CLIPS_DIRNAME = "ArchivedClips"
_DEFAULT_SAMPLE_RATE = 30
_DEFAULT_TRIP_GAP_MINUTES = 5
_DEFAULT_INDEX_TOO_NEW_SECONDS = 120.0
_DEFAULT_HARSH_BRAKE_THRESHOLD = -4.0
_DEFAULT_EMERGENCY_BRAKE_THRESHOLD = -7.0
_DEFAULT_HARD_ACCEL_THRESHOLD = 3.5
_DEFAULT_SHARP_TURN_LATERAL_MPS2 = 4.0
_DEFAULT_SPEED_LIMIT_MPS = 35.76
_DEFAULT_STALE_SCAN_INTERVAL_SECONDS = 30 * 24 * 60 * 60
_DEFAULT_STALE_SCAN_JITTER_SECONDS = 24 * 60 * 60
_DEFAULT_INITIAL_STALE_SCAN_BASE_SECONDS = 5 * 60
_DEFAULT_INITIAL_STALE_SCAN_JITTER_SECONDS = 5 * 60
_DEFAULT_STALE_SCAN_DEBOUNCE_SECONDS = 10 * 60
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_INDEXER_STATUS_KEY = "indexer_status"


class MappingServiceError(RuntimeError):
    """Base error raised by the mapping domain service."""


class IndexerError(MappingServiceError):
    """Indexing or queue orchestration failed."""


class DiagnoseError(MappingServiceError):
    """Video diagnostics could not be completed safely."""


class IndexOutcome(StrEnum):
    """Structured result codes for indexing a single file."""

    INDEXED = "indexed"
    ALREADY_INDEXED = "already_indexed"
    DUPLICATE_UPGRADED = "duplicate_upgraded"
    NO_GPS_RECORDED = "no_gps_recorded"
    NOT_FRONT_CAMERA = "not_front_camera"
    TOO_NEW = "too_new"
    FILE_MISSING = "file_missing"
    PARSE_ERROR = "parse_error"
    DB_BUSY = "db_busy"


_TERMINAL_OUTCOMES = frozenset(
    {
        IndexOutcome.INDEXED,
        IndexOutcome.ALREADY_INDEXED,
        IndexOutcome.DUPLICATE_UPGRADED,
        IndexOutcome.NO_GPS_RECORDED,
        IndexOutcome.NOT_FRONT_CAMERA,
        IndexOutcome.FILE_MISSING,
    }
)


@dataclass(frozen=True, slots=True)
class IndexResult:
    """Outcome returned from :meth:`MappingService.index_single_file`."""

    outcome: IndexOutcome
    waypoints: int = 0
    events: int = 0
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.outcome in _TERMINAL_OUTCOMES


@dataclass(frozen=True, slots=True)
class MappingServiceConfig:
    """Constructor-injected runtime settings for :class:`MappingService`."""

    db_path: Path
    backup_dir: Path
    media_root: Path
    archive_root: Path
    backup_retention: int = 3
    archived_clips_dirname: str = _DEFAULT_ARCHIVED_CLIPS_DIRNAME
    sample_rate: int = _DEFAULT_SAMPLE_RATE
    trip_gap_minutes: int = _DEFAULT_TRIP_GAP_MINUTES
    index_too_new_seconds: float = _DEFAULT_INDEX_TOO_NEW_SECONDS
    harsh_brake_threshold: float = _DEFAULT_HARSH_BRAKE_THRESHOLD
    emergency_brake_threshold: float = _DEFAULT_EMERGENCY_BRAKE_THRESHOLD
    hard_accel_threshold: float = _DEFAULT_HARD_ACCEL_THRESHOLD
    sharp_turn_lateral_mps2: float = _DEFAULT_SHARP_TURN_LATERAL_MPS2
    speed_limit_mps: float = _DEFAULT_SPEED_LIMIT_MPS
    stale_scan_interval_seconds: float = _DEFAULT_STALE_SCAN_INTERVAL_SECONDS
    stale_scan_jitter_seconds: float = _DEFAULT_STALE_SCAN_JITTER_SECONDS
    initial_stale_scan_base_seconds: float = _DEFAULT_INITIAL_STALE_SCAN_BASE_SECONDS
    initial_stale_scan_jitter_seconds: float = _DEFAULT_INITIAL_STALE_SCAN_JITTER_SECONDS
    stale_scan_debounce_seconds: float = _DEFAULT_STALE_SCAN_DEBOUNCE_SECONDS
    shutdown_timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.backup_retention <= 0:
            raise ValueError("backup_retention must be > 0")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be > 0")
        if self.trip_gap_minutes <= 0:
            raise ValueError("trip_gap_minutes must be > 0")
        if self.index_too_new_seconds <= 0:
            raise ValueError("index_too_new_seconds must be > 0")
        if self.stale_scan_interval_seconds <= 0:
            raise ValueError("stale_scan_interval_seconds must be > 0")
        if self.stale_scan_debounce_seconds <= 0:
            raise ValueError("stale_scan_debounce_seconds must be > 0")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be > 0")
        if not self.archived_clips_dirname.strip():
            raise ValueError("archived_clips_dirname must be non-empty")

    @property
    def event_thresholds(self) -> dict[str, float]:
        return {
            "harsh_brake_threshold": self.harsh_brake_threshold,
            "emergency_brake_threshold": self.emergency_brake_threshold,
            "hard_accel_threshold": self.hard_accel_threshold,
            "sharp_turn_lateral_mps2": self.sharp_turn_lateral_mps2,
            "speed_limit_mps": self.speed_limit_mps,
        }


@dataclass(slots=True)
class _IndexerStatusState:
    running: bool = False
    queue_depth: int = 0
    files_done_session: int = 0
    active_file: str | None = None
    source: str | None = None
    last_drained_at: str | None = None
    last_error: str | None = None
    last_result: str | None = None


class MappingService:
    """B-1 mapping service facade.

    The long-term indexing home is the Rust worker, but Phase 5.13c keeps the
    Python fallback here so the web app reaches v1 mapping parity before the
    worker-side replacement lands.
    """

    def __init__(
        self,
        *,
        config: MappingServiceConfig,
        migrations_runner: MigrationsRunner | None = None,
        queries: MappingQueries | None = None,
        parser: SeiParserProtocol | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._migrations_runner = migrations_runner or MigrationsRunner(
            MigrationsConfig(
                db_path=config.db_path,
                backup_dir=config.backup_dir,
                backup_retention=config.backup_retention,
            )
        )
        self._queries = queries or MappingQueries(
            config=MappingQueriesConfig(
                db_path=config.db_path,
                backup_dir=config.backup_dir,
                backup_retention=config.backup_retention,
                media_root=config.media_root,
                archived_clips_dirname=config.archived_clips_dirname,
            ),
            migrations_runner=self._migrations_runner,
        )
        self._parser = parser
        self._monotonic = monotonic
        self._status_lock = threading.RLock()
        self._status = self._load_status_state()
        self._daily_stale_scan_thread: threading.Thread | None = None
        self._daily_stale_scan_stop: threading.Event | None = None
        self._last_stale_scan_at = 0.0

    @property
    def config(self) -> MappingServiceConfig:
        return self._config

    @property
    def queries(self) -> MappingQueries:
        return self._queries

    def get_db_connection(self) -> sqlite3.Connection:
        return self._migrations_runner.init_db()

    @contextmanager
    def open_db(self) -> Iterator[sqlite3.Connection]:
        with self._migrations_runner.open_db() as connection:
            yield connection

    def get_indexer_status(self) -> dict[str, object]:
        state = self._status_dict()
        state["queue_depth"] = self._queue_depth()
        return state

    def get_stats(self) -> Stats:
        return replace(
            self._queries.get_stats(),
            indexer_status=json.dumps(self.get_indexer_status(), sort_keys=True),
        )

    def index_single_file(self, video_path: str | Path, *, source: str = "manual") -> IndexResult:
        from .indexer import index_single_file  # noqa: PLC0415

        path = Path(video_path)
        self._set_status(running=True, active_file=str(path), source=source)
        result = index_single_file(service=self, video_path=path)
        payload: dict[str, object] = {
            "running": False,
            "active_file": None,
            "last_result": result.outcome.value,
        }
        if result.error is not None:
            payload["last_error"] = result.error
        if result.outcome == IndexOutcome.INDEXED:
            payload["files_done_session"] = self._status.files_done_session + 1
            payload["last_drained_at"] = self._utc_now()
        self._set_status(**payload)
        return result

    def purge_deleted_videos(
        self,
        *,
        deleted_paths: tuple[str | Path, ...] | None = None,
    ) -> dict[str, int]:
        from .purge import purge_deleted_videos  # noqa: PLC0415

        return purge_deleted_videos(service=self, deleted_paths=deleted_paths)

    def boot_catchup_scan(self, *, source: str = "catchup") -> dict[str, int]:
        from .stale_scan import boot_catchup_scan  # noqa: PLC0415

        return boot_catchup_scan(service=self, source=source)

    def trigger_stale_scan_now(
        self,
        *,
        source: str = "manual",
        debounce_seconds: float | None = None,
    ) -> dict[str, float | str]:
        from .stale_scan import trigger_stale_scan_now  # noqa: PLC0415

        return trigger_stale_scan_now(
            service=self,
            source=source,
            debounce_seconds=debounce_seconds,
        )

    def reset_stale_scan_state_for_tests(self) -> None:
        with self._status_lock:
            self._last_stale_scan_at = 0.0

    def start_daily_stale_scan(self) -> bool:
        from .stale_scan import start_daily_stale_scan  # noqa: PLC0415

        return start_daily_stale_scan(self)

    def stop_daily_stale_scan(self, *, timeout: float | None = None) -> bool:
        from .stale_scan import stop_daily_stale_scan  # noqa: PLC0415

        return stop_daily_stale_scan(self, timeout=timeout)

    def diagnose_video(
        self,
        teslacam_path: str | Path | None = None,
        *,
        max_videos: int = 3,
    ) -> dict[str, object]:
        from .diagnose import diagnose_video  # noqa: PLC0415

        root = self._config.media_root if teslacam_path is None else Path(teslacam_path)
        return diagnose_video(service=self, teslacam_path=root, max_videos=max_videos)

    def shutdown(self, *, timeout: float | None = None) -> bool:
        return self.stop_daily_stale_scan(timeout=timeout)

    def _parser_or_default(self) -> SeiParserProtocol | None:
        return self._parser

    def _set_status(self, **changes: object) -> None:
        with self._status_lock:
            self._apply_status_changes(changes)
            self._status.queue_depth = self._queue_depth()
            self._persist_status_locked()

    def _status_dict(self) -> dict[str, object]:
        with self._status_lock:
            return dict(asdict(self._status))

    def _queue_depth(self) -> int:
        try:
            with self.open_db() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM indexing_queue WHERE claimed_by IS NULL"
                ).fetchone()
        except sqlite3.Error:
            return self._status.queue_depth
        if row is None:
            return 0
        value = row["count"] if isinstance(row, sqlite3.Row) else row[0]
        return int(float(value)) if isinstance(value, Real) else 0

    def _persist_status_locked(self) -> None:
        payload = json.dumps(asdict(self._status), sort_keys=True)
        try:
            with self.open_db() as connection:
                _kv_set(connection, _INDEXER_STATUS_KEY, payload)
        except sqlite3.Error as exc:
            logger.debug("Failed to persist indexer status: %s", exc)

    def _load_status_state(self) -> _IndexerStatusState:
        try:
            with self.open_db() as connection:
                payload = _kv_get(connection, _INDEXER_STATUS_KEY)
        except sqlite3.Error:
            return _IndexerStatusState()
        if payload is None:
            return _IndexerStatusState()
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError:
            return _IndexerStatusState()
        if not isinstance(raw, dict):
            return _IndexerStatusState()
        state = _IndexerStatusState()
        self._apply_status_changes(raw, state=state)
        return state

    def _utc_now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _apply_status_changes(
        self,
        changes: dict[str, object],
        *,
        state: _IndexerStatusState | None = None,
    ) -> None:
        target = self._status if state is None else state
        for key, value in changes.items():
            if key == "running" and isinstance(value, bool):
                target.running = value
            elif key == "queue_depth" and isinstance(value, Real):
                target.queue_depth = int(float(value))
            elif key == "files_done_session" and isinstance(value, Real):
                target.files_done_session = int(float(value))
            elif key == "active_file" and (isinstance(value, str) or value is None):
                target.active_file = value
            elif key == "source" and (isinstance(value, str) or value is None):
                target.source = value
            elif key == "last_drained_at" and (isinstance(value, str) or value is None):
                target.last_drained_at = value
            elif key == "last_error" and (isinstance(value, str) or value is None):
                target.last_error = value
            elif key == "last_result" and (isinstance(value, str) or value is None):
                target.last_result = value


def make_mapping_service(cfg: WebConfig | MappingServiceConfig) -> MappingService:
    """Build :class:`MappingService` from app config or explicit settings."""

    if isinstance(cfg, MappingServiceConfig):
        return MappingService(config=cfg)
    config = MappingServiceConfig(
        db_path=cfg.mapping.db_path,
        backup_dir=cfg.mapping.backup_dir,
        backup_retention=cfg.mapping.backup_retention,
        media_root=cfg.mapping.media_root,
        archive_root=cfg.mapping.archive_root,
        archived_clips_dirname=cfg.mapping.archived_clips_dirname,
        sample_rate=cfg.mapping.sample_rate,
        trip_gap_minutes=cfg.mapping.trip_gap_minutes,
        index_too_new_seconds=cfg.mapping.index_too_new_seconds,
        harsh_brake_threshold=cfg.mapping.harsh_brake_threshold,
        emergency_brake_threshold=cfg.mapping.emergency_brake_threshold,
        hard_accel_threshold=cfg.mapping.hard_accel_threshold,
        sharp_turn_lateral_mps2=cfg.mapping.sharp_turn_lateral_mps2,
        speed_limit_mps=cfg.mapping.speed_limit_mps,
        stale_scan_interval_seconds=cfg.mapping.stale_scan_interval_seconds,
        stale_scan_jitter_seconds=cfg.mapping.stale_scan_jitter_seconds,
        initial_stale_scan_base_seconds=cfg.mapping.initial_stale_scan_base_seconds,
        initial_stale_scan_jitter_seconds=cfg.mapping.initial_stale_scan_jitter_seconds,
        stale_scan_debounce_seconds=cfg.mapping.stale_scan_debounce_seconds,
    )
    migrations_runner = make_migrations_runner(cfg)
    return MappingService(
        config=config,
        migrations_runner=migrations_runner,
        queries=MappingQueries(
            config=MappingQueriesConfig(
                db_path=config.db_path,
                backup_dir=config.backup_dir,
                backup_retention=config.backup_retention,
                media_root=config.media_root,
                archived_clips_dirname=config.archived_clips_dirname,
            ),
            migrations_runner=migrations_runner,
        ),
    )
