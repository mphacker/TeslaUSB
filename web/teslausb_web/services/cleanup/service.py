from __future__ import annotations

import logging
import random
import string
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from teslausb_web.services.cleanup.discovery import (
    ClipGroup,
    MappingSnapshot,
    OrphanDetails,
    discover_clip_groups,
    load_mapping_snapshot,
    scan_orphans,
)
from teslausb_web.services.cleanup.execute import DeletionResult, execute_groups
from teslausb_web.services.cleanup.preview import PreviewPlan, build_preview_plan
from teslausb_web.services.cleanup.report import (
    StoredRunRecord,
    finish_run_record,
    load_recent_run_records,
    load_run_record,
    start_run_record,
)
from teslausb_web.services.storage_retention_service import RetentionPolicy, RetentionPreviewSummary

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)
_RANDOM = random.SystemRandom()
_RUN_ID_ALPHABET = string.ascii_lowercase + string.digits


class CleanupError(RuntimeError):
    """Base cleanup-service failure."""


class CleanupConfigError(ValueError):
    """Cleanup-service configuration is invalid."""


class CleanupCancelledError(CleanupError):
    """A running cleanup job was cancelled."""


@dataclass(frozen=True, slots=True)
class OrphanScan:
    db_only_paths: tuple[str, ...]
    fs_only_paths: tuple[str, ...]
    total_bytes_recoverable: int


@dataclass(frozen=True, slots=True)
class CleanupPreview:
    counts_by_category: dict[str, int]
    bytes_total: int
    sample_paths: tuple[str, ...]
    generated_at: datetime
    current_free_pct: float = 0.0
    projected_free_pct: float = 0.0
    current_free_bytes: int = 0
    current_used_bytes: int = 0
    total_capacity_bytes: int = 0
    bytes_by_category: dict[str, int] = field(default_factory=dict)
    candidate_count: int = 0
    protected_count: int = 0
    orphan_scan: OrphanScan | None = None


@dataclass(frozen=True, slots=True)
class CleanupRun:
    run_id: str
    status: str
    action: str
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None
    deleted_count: int
    deleted_bytes: int
    errors: tuple[str, ...]
    policy_snapshot: dict[str, object]
    counts_by_category: dict[str, int] = field(default_factory=dict)
    sample_paths: tuple[str, ...] = ()
    generated_at: datetime | None = None
    current_path: str | None = None
    total_candidates: int = 0
    processed_candidates: int = 0
    orphan_scan: OrphanScan | None = None


@dataclass(frozen=True, slots=True)
class CleanupRunStatus:
    run: CleanupRun
    active: bool


@dataclass(frozen=True, slots=True)
class CleanupReport:
    recent_runs: tuple[CleanupRun, ...]


@dataclass(frozen=True, slots=True)
class CleanupConfig:
    history_db_path: Path
    media_root: Path
    max_concurrent_runs: int = 1
    dry_run_default: bool = True
    orphan_scan_batch_size: int = 500
    sample_path_limit: int = 12
    recent_protection_hours: int = 1
    delete_gps_tagged_clips: bool = False
    orphan_min_age_seconds: int = 300
    report_limit: int = 20

    def __post_init__(self) -> None:
        for path_name, path_value in (
            ("history_db_path", self.history_db_path),
            ("media_root", self.media_root),
        ):
            posix_path = PurePosixPath(path_value.as_posix())
            if not path_value.is_absolute() and not posix_path.is_absolute():
                raise CleanupConfigError(f"{path_name} must be absolute, got {path_value!r}")
        for numeric_name, numeric_value in (
            ("max_concurrent_runs", self.max_concurrent_runs),
            ("orphan_scan_batch_size", self.orphan_scan_batch_size),
            ("sample_path_limit", self.sample_path_limit),
            ("recent_protection_hours", self.recent_protection_hours),
            ("orphan_min_age_seconds", self.orphan_min_age_seconds),
            ("report_limit", self.report_limit),
        ):
            if numeric_value <= 0:
                raise CleanupConfigError(f"{numeric_name} must be > 0")


class RetentionSource(Protocol):
    def get_policy(self) -> RetentionPolicy: ...


class MappingAccess(Protocol):
    def open_db(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def purge_deleted_videos(
        self,
        *,
        deleted_paths: tuple[str | Path, ...] | None = None,
    ) -> dict[str, int]: ...

    def trigger_stale_scan_now(
        self,
        *,
        source: str = "manual",
        debounce_seconds: float | None = None,
    ) -> dict[str, float | str]: ...


@dataclass(slots=True)
class _ActiveRun:
    run_id: str
    action: str
    dry_run: bool
    started_at: datetime
    policy_snapshot: dict[str, object]
    counts_by_category: dict[str, int]
    sample_paths: tuple[str, ...]
    generated_at: datetime
    total_candidates: int
    orphan_scan: OrphanScan | None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    status: str = "running"
    finished_at: datetime | None = None
    deleted_count: int = 0
    deleted_bytes: int = 0
    processed_candidates: int = 0
    current_path: str | None = None
    errors: list[str] = field(default_factory=list)


class CleanupService:
    def __init__(
        self,
        *,
        config: CleanupConfig,
        retention_source: RetentionSource,
        mapping_access: MappingAccess,
        cache_invalidator: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._retention_source = retention_source
        self._mapping_access = mapping_access
        self._cache_invalidator = cache_invalidator
        self._lock = threading.RLock()
        self._active_runs: dict[str, _ActiveRun] = {}

    @property
    def config(self) -> CleanupConfig:
        return self._config

    def preview(self) -> CleanupPreview:
        policy = self._retention_source.get_policy()
        preview_plan, orphan_details = self._build_preview_state(policy)
        return self._preview_from_plan(preview_plan, orphan_details.scan)

    def preview_summary(self) -> RetentionPreviewSummary:
        _ = self.preview()
        return RetentionPreviewSummary(preview_available=True, deferred_reason="")

    def report(self, limit: int | None = None) -> CleanupReport:
        recent_records = load_recent_run_records(
            self._config,
            limit or self._config.report_limit,
        )
        recent_runs = tuple(self._run_from_record(record) for record in recent_records)
        return CleanupReport(recent_runs=recent_runs)

    def get_run_status(self, run_id: str) -> CleanupRunStatus:
        with self._lock:
            active = self._active_runs.get(run_id)
            if active is not None:
                return CleanupRunStatus(run=self._run_from_active(active), active=True)
        record = load_run_record(self._config, run_id)
        if record is None:
            raise CleanupError(f"Unknown cleanup run: {run_id}")
        return CleanupRunStatus(run=self._run_from_record(record), active=False)

    def start_execute(self, *, dry_run: bool | None = None) -> str:
        selected_dry_run = self._config.dry_run_default if dry_run is None else dry_run
        policy = self._retention_source.get_policy()
        preview_plan, orphan_details = self._build_preview_state(policy)
        run_id = _generate_run_id()
        policy_snapshot = _policy_snapshot(policy)
        run_state = _ActiveRun(
            run_id=run_id,
            action="cleanup",
            dry_run=selected_dry_run,
            started_at=_utc_now(),
            policy_snapshot=policy_snapshot,
            counts_by_category=dict(preview_plan.counts_by_category),
            sample_paths=preview_plan.sample_paths,
            generated_at=preview_plan.generated_at,
            total_candidates=preview_plan.candidate_count,
            orphan_scan=orphan_details.scan,
        )
        with self._lock:
            if self._running_count() >= self._config.max_concurrent_runs:
                raise CleanupError("cleanup run already in progress")
            self._active_runs[run_id] = run_state
        start_run_record(
            self._config,
            run_id=run_id,
            action="cleanup",
            dry_run=selected_dry_run,
            started_at=run_state.started_at.isoformat(),
            generated_at=preview_plan.generated_at.isoformat(),
            policy_snapshot=policy_snapshot,
            counts_by_category=preview_plan.counts_by_category,
            sample_paths=preview_plan.sample_paths,
            total_candidates=preview_plan.candidate_count,
            orphan_db_only_paths=orphan_details.scan.db_only_paths,
            orphan_fs_only_paths=orphan_details.scan.fs_only_paths,
            orphan_bytes_total=orphan_details.scan.total_bytes_recoverable,
        )
        if not preview_plan.candidate_groups:
            run_state.status = "completed"
            run_state.finished_at = _utc_now()
            run_state.processed_candidates = 0
            finish_run_record(self._config, self._record_from_active(run_state))
            with self._lock:
                self._active_runs.pop(run_id, None)
            return run_id
        thread = threading.Thread(
            target=self._run_cleanup_thread,
            args=(run_state, preview_plan.candidate_groups),
            name=f"cleanup-run-{run_id}",
            daemon=True,
        )
        run_state.thread = thread
        thread.start()
        return run_id

    def purge_orphans(self) -> CleanupRun:
        policy = self._retention_source.get_policy()
        _preview_plan, orphan_details = self._build_preview_state(policy)
        run_id = _generate_run_id()
        started_at = _utc_now()
        policy_snapshot = _policy_snapshot(policy)
        start_run_record(
            self._config,
            run_id=run_id,
            action="orphan-purge",
            dry_run=False,
            started_at=started_at.isoformat(),
            generated_at=started_at.isoformat(),
            policy_snapshot=policy_snapshot,
            counts_by_category={},
            sample_paths=(),
            total_candidates=len(orphan_details.fs_only_groups) + len(orphan_details.db_only_paths),
            orphan_db_only_paths=orphan_details.scan.db_only_paths,
            orphan_fs_only_paths=orphan_details.scan.fs_only_paths,
            orphan_bytes_total=orphan_details.scan.total_bytes_recoverable,
        )
        errors: list[str] = []
        deleted_count = 0
        deleted_bytes = 0
        if orphan_details.fs_only_groups:
            outcome = execute_groups(
                self._config,
                orphan_details.fs_only_groups,
                dry_run=False,
                cancel_event=threading.Event(),
            )
            deleted_count += outcome.deleted_count
            deleted_bytes += outcome.deleted_bytes
            errors.extend(outcome.errors)
        if orphan_details.db_only_paths:
            try:
                self._mapping_access.purge_deleted_videos(
                    deleted_paths=tuple(orphan_details.db_only_paths)
                )
            except Exception as exc:
                logger.exception("Failed to purge dangling mapping rows")
                errors.append(f"Failed to purge dangling mapping rows: {exc}")
            else:
                deleted_count += len(orphan_details.db_only_paths)
        if orphan_details.fs_only_groups or orphan_details.db_only_paths:
            try:
                self._mapping_access.trigger_stale_scan_now(
                    source="cleanup-orphan-purge",
                    debounce_seconds=0.0,
                )
            except Exception:
                logger.exception("Failed to trigger stale scan after orphan purge")
        finished_at = _utc_now()
        total_candidates = len(orphan_details.fs_only_groups) + len(orphan_details.db_only_paths)
        run = CleanupRun(
            run_id=run_id,
            status="completed" if not errors else "failed",
            action="orphan-purge",
            dry_run=False,
            started_at=started_at,
            finished_at=finished_at,
            deleted_count=deleted_count,
            deleted_bytes=deleted_bytes,
            errors=tuple(errors),
            policy_snapshot=policy_snapshot,
            generated_at=started_at,
            total_candidates=total_candidates,
            processed_candidates=total_candidates,
            orphan_scan=orphan_details.scan,
        )
        finish_run_record(self._config, self._record_from_run(run))
        return run

    def shutdown(self, timeout: float = 5.0) -> bool:
        with self._lock:
            active_runs = tuple(self._active_runs.values())
        for run_state in active_runs:
            run_state.cancel_event.set()
        clean = True
        for run_state in active_runs:
            thread = run_state.thread
            if thread is None:
                continue
            thread.join(timeout=timeout)
            clean = clean and not thread.is_alive()
        return clean

    def _build_preview_state(self, policy: RetentionPolicy) -> tuple[PreviewPlan, OrphanDetails]:
        snapshot = self._mapping_snapshot()
        groups = discover_clip_groups(self._config, snapshot)
        orphan_details = scan_orphans(self._config, snapshot, groups)
        preview_plan = build_preview_plan(self._config, policy, groups)
        return preview_plan, orphan_details

    def _mapping_snapshot(self) -> MappingSnapshot:
        return load_mapping_snapshot(self._mapping_access.open_db)

    def _preview_from_plan(
        self,
        preview_plan: PreviewPlan,
        orphan_scan: OrphanScan,
    ) -> CleanupPreview:
        return CleanupPreview(
            counts_by_category=dict(preview_plan.counts_by_category),
            bytes_total=preview_plan.bytes_total,
            sample_paths=preview_plan.sample_paths,
            generated_at=preview_plan.generated_at,
            current_free_pct=preview_plan.current_free_pct,
            projected_free_pct=preview_plan.projected_free_pct,
            current_free_bytes=preview_plan.current_free_bytes,
            current_used_bytes=preview_plan.current_used_bytes,
            total_capacity_bytes=preview_plan.total_capacity_bytes,
            bytes_by_category=dict(preview_plan.bytes_by_category),
            candidate_count=preview_plan.candidate_count,
            protected_count=preview_plan.protected_count,
            orphan_scan=orphan_scan,
        )

    def _run_cleanup_thread(
        self,
        run_state: _ActiveRun,
        groups: tuple[ClipGroup, ...],
    ) -> None:
        def _progress(
            group: ClipGroup,
            processed: int,
            deleted_count: int,
            deleted_bytes: int,
            errors: tuple[str, ...],
        ) -> None:
            self._update_active_run(
                run_state.run_id,
                current_path=group.display_path,
                processed_candidates=processed,
                deleted_count=deleted_count,
                deleted_bytes=deleted_bytes,
                errors=list(errors),
            )

        try:
            result = execute_groups(
                self._config,
                groups,
                dry_run=run_state.dry_run,
                cancel_event=run_state.cancel_event,
                progress=_progress,
            )
            self._finish_active_run(run_state, result)
        except CleanupCancelledError as exc:
            logger.info("Cleanup run %s cancelled", run_state.run_id)
            self._finish_active_run(run_state, None, status="cancelled", extra_errors=(str(exc),))
        except Exception as exc:
            logger.exception("Cleanup run %s failed", run_state.run_id)
            self._finish_active_run(run_state, None, status="failed", extra_errors=(str(exc),))

    def _finish_active_run(
        self,
        run_state: _ActiveRun,
        result: DeletionResult | None,
        *,
        status: str = "completed",
        extra_errors: tuple[str, ...] = (),
    ) -> None:
        run_state.status = status
        run_state.finished_at = _utc_now()
        if result is not None:
            run_state.deleted_count = result.deleted_count
            run_state.deleted_bytes = result.deleted_bytes
            run_state.processed_candidates = run_state.total_candidates
            run_state.errors = list(result.errors)
        run_state.errors.extend(extra_errors)
        if run_state.errors and run_state.status == "completed":
            run_state.status = "failed"
        record = self._record_from_active(run_state)
        finish_run_record(self._config, record)
        if (
            result is not None
            and not run_state.dry_run
            and run_state.status == "completed"
            and run_state.deleted_count > 0
            and self._cache_invalidator is not None
        ):
            self._cache_invalidator()
        with self._lock:
            self._active_runs.pop(run_state.run_id, None)

    def _update_active_run(self, run_id: str, **changes: object) -> None:
        with self._lock:
            active = self._active_runs.get(run_id)
            if active is None:
                return
            for key, value in changes.items():
                if key == "current_path" and (isinstance(value, str) or value is None):
                    active.current_path = value
                elif key == "processed_candidates" and isinstance(value, int):
                    active.processed_candidates = value
                elif key == "deleted_count" and isinstance(value, int):
                    active.deleted_count = value
                elif key == "deleted_bytes" and isinstance(value, int):
                    active.deleted_bytes = value
                elif key == "errors" and isinstance(value, list):
                    active.errors = [item for item in value if isinstance(item, str)]
            finish_run_record(self._config, self._record_from_active(active))

    def _running_count(self) -> int:
        return sum(
            1 for run in self._active_runs.values() if run.thread is None or run.thread.is_alive()
        )

    def _record_from_active(self, active: _ActiveRun) -> StoredRunRecord:
        return StoredRunRecord(
            run_id=active.run_id,
            action=active.action,
            status=active.status,
            dry_run=active.dry_run,
            started_at=active.started_at.isoformat(),
            finished_at=None if active.finished_at is None else active.finished_at.isoformat(),
            deleted_count=active.deleted_count,
            deleted_bytes=active.deleted_bytes,
            errors=tuple(active.errors),
            policy_snapshot=dict(active.policy_snapshot),
            counts_by_category=dict(active.counts_by_category),
            sample_paths=active.sample_paths,
            generated_at=active.generated_at.isoformat(),
            current_path=active.current_path,
            total_candidates=active.total_candidates,
            processed_candidates=active.processed_candidates,
            orphan_db_only_paths=()
            if active.orphan_scan is None
            else active.orphan_scan.db_only_paths,
            orphan_fs_only_paths=()
            if active.orphan_scan is None
            else active.orphan_scan.fs_only_paths,
            orphan_bytes_total=0
            if active.orphan_scan is None
            else active.orphan_scan.total_bytes_recoverable,
        )

    def _run_from_active(self, active: _ActiveRun) -> CleanupRun:
        return CleanupRun(
            run_id=active.run_id,
            status=active.status,
            action=active.action,
            dry_run=active.dry_run,
            started_at=active.started_at,
            finished_at=active.finished_at,
            deleted_count=active.deleted_count,
            deleted_bytes=active.deleted_bytes,
            errors=tuple(active.errors),
            policy_snapshot=dict(active.policy_snapshot),
            counts_by_category=dict(active.counts_by_category),
            sample_paths=active.sample_paths,
            generated_at=active.generated_at,
            current_path=active.current_path,
            total_candidates=active.total_candidates,
            processed_candidates=active.processed_candidates,
            orphan_scan=active.orphan_scan,
        )

    def _run_from_record(self, record: StoredRunRecord) -> CleanupRun:
        orphan_scan = None
        if record.orphan_db_only_paths or record.orphan_fs_only_paths or record.orphan_bytes_total:
            orphan_scan = OrphanScan(
                db_only_paths=record.orphan_db_only_paths,
                fs_only_paths=record.orphan_fs_only_paths,
                total_bytes_recoverable=record.orphan_bytes_total,
            )
        return CleanupRun(
            run_id=record.run_id,
            status=record.status,
            action=record.action,
            dry_run=record.dry_run,
            started_at=datetime.fromisoformat(record.started_at),
            finished_at=None
            if record.finished_at is None
            else datetime.fromisoformat(record.finished_at),
            deleted_count=record.deleted_count,
            deleted_bytes=record.deleted_bytes,
            errors=record.errors,
            policy_snapshot=dict(record.policy_snapshot),
            counts_by_category=dict(record.counts_by_category),
            sample_paths=record.sample_paths,
            generated_at=datetime.fromisoformat(record.generated_at),
            current_path=record.current_path,
            total_candidates=record.total_candidates,
            processed_candidates=record.processed_candidates,
            orphan_scan=orphan_scan,
        )

    def _record_from_run(self, run: CleanupRun) -> StoredRunRecord:
        return StoredRunRecord(
            run_id=run.run_id,
            action=run.action,
            status=run.status,
            dry_run=run.dry_run,
            started_at=run.started_at.isoformat(),
            finished_at=None if run.finished_at is None else run.finished_at.isoformat(),
            deleted_count=run.deleted_count,
            deleted_bytes=run.deleted_bytes,
            errors=run.errors,
            policy_snapshot=dict(run.policy_snapshot),
            counts_by_category=dict(run.counts_by_category),
            sample_paths=run.sample_paths,
            generated_at=(
                _utc_now().isoformat() if run.generated_at is None else run.generated_at.isoformat()
            ),
            current_path=run.current_path,
            total_candidates=run.total_candidates,
            processed_candidates=run.processed_candidates,
            orphan_db_only_paths=() if run.orphan_scan is None else run.orphan_scan.db_only_paths,
            orphan_fs_only_paths=() if run.orphan_scan is None else run.orphan_scan.fs_only_paths,
            orphan_bytes_total=0
            if run.orphan_scan is None
            else run.orphan_scan.total_bytes_recoverable,
        )


class _RetentionSerializer(Protocol):
    def serialize_policy(self, policy: RetentionPolicy) -> dict[str, object]: ...


def make_cleanup_service(
    cfg: WebConfig | CleanupConfig,
    retention_svc: RetentionSource,
    archive_queries_or_db_factory: MappingAccess,
    cache_invalidator: Callable[[], None] | None,
) -> CleanupService:
    config = (
        cfg
        if isinstance(cfg, CleanupConfig)
        else CleanupConfig(
            history_db_path=cfg.cleanup.history_db_path,
            media_root=cfg.mapping.media_root,
            max_concurrent_runs=cfg.cleanup.max_concurrent_runs,
            dry_run_default=cfg.cleanup.dry_run_default,
            orphan_scan_batch_size=cfg.cleanup.orphan_scan_batch_size,
            sample_path_limit=cfg.cleanup.sample_path_limit,
            recent_protection_hours=cfg.cleanup.recent_protection_hours,
            delete_gps_tagged_clips=cfg.cleanup.delete_gps_tagged_clips,
            orphan_min_age_seconds=cfg.cleanup.orphan_min_age_seconds,
            report_limit=cfg.cleanup.report_limit,
        )
    )
    return CleanupService(
        config=config,
        retention_source=retention_svc,
        mapping_access=archive_queries_or_db_factory,
        cache_invalidator=cache_invalidator,
    )


def _policy_snapshot(policy: RetentionPolicy) -> dict[str, object]:
    return {
        "max_age_days": policy.max_age_days,
        "target_free_pct": policy.target_free_pct,
        "max_archive_size_gb": policy.max_archive_size_gb,
        "short_retention_warning_days": policy.short_retention_warning_days,
        "keep_recent_clips": policy.keep_recent_clips,
        "keep_saved_clips": policy.keep_saved_clips,
        "keep_event_clips": policy.keep_event_clips,
        "keep_encrypted_clips": policy.keep_encrypted_clips,
        "keep_archived_clips": policy.keep_archived_clips,
        "dry_run": policy.dry_run,
        "recent_clips_days": policy.recent_clips_days,
        "saved_clips_days": policy.saved_clips_days,
        "event_clips_days": policy.event_clips_days,
        "encrypted_clips_days": policy.encrypted_clips_days,
        "archived_clips_days": policy.archived_clips_days,
    }


def _generate_run_id() -> str:
    return "run-" + "".join(_RANDOM.choice(_RUN_ID_ALPHABET) for _ in range(12))


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
