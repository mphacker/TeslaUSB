from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
from teslausb_web.config import (
    CleanupSection,
    MappingSection,
    PathsSection,
    StorageRetentionSection,
    WebConfig,
    WebSection,
)
from teslausb_web.services.cleanup.discovery import (
    MappingSnapshot,
    discover_clip_groups,
    load_mapping_snapshot,
    scan_orphans,
)
from teslausb_web.services.cleanup.execute import DeletionResult, execute_groups
from teslausb_web.services.cleanup.preview import build_preview_plan
from teslausb_web.services.cleanup.report import (
    finish_run_record,
    load_recent_run_records,
    load_run_record,
    start_run_record,
)
from teslausb_web.services.cleanup.service import (
    CleanupCancelledError,
    CleanupConfig,
    CleanupConfigError,
    CleanupError,
    CleanupService,
    _policy_snapshot,
    make_cleanup_service,
)
from teslausb_web.services.storage_retention_service import (
    RetentionPolicy,
    make_storage_retention_service,
)

_CATEGORY_FOLDERS = {
    "recent": "RecentClips",
    "saved": "SavedClips",
    "event": "SentryClips",
    "encrypted": "EncryptedClips",
}


class FakeRetentionSource:
    def __init__(self, policy: RetentionPolicy | None = None) -> None:
        self.policy = policy or RetentionPolicy()

    def get_policy(self) -> RetentionPolicy:
        return self.policy


class FakeMappingAccess:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.purge_calls: list[tuple[str, ...] | None] = []
        self.stale_calls: list[tuple[str, float | None]] = []
        self.raise_purge: Exception | None = None
        self.raise_stale: Exception | None = None
        _init_mapping_db(db_path)

    @contextmanager
    def open_db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def purge_deleted_videos(
        self,
        *,
        deleted_paths: tuple[str | Path, ...] | None = None,
    ) -> dict[str, int]:
        self.purge_calls.append(
            None if deleted_paths is None else tuple(str(path) for path in deleted_paths)
        )
        if self.raise_purge is not None:
            raise self.raise_purge
        return {"purged": 0 if deleted_paths is None else len(deleted_paths)}

    def trigger_stale_scan_now(
        self,
        *,
        source: str = "manual",
        debounce_seconds: float | None = None,
    ) -> dict[str, float | str]:
        self.stale_calls.append((source, debounce_seconds))
        if self.raise_stale is not None:
            raise self.raise_stale
        return {
            "source": source,
            "debounce_seconds": 0.0 if debounce_seconds is None else debounce_seconds,
        }


class InlineThread:
    def __init__(
        self,
        _group: object | None = None,
        target: object | None = None,
        name: str | None = None,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
        daemon: bool | None = None,  # noqa: FBT001
    ) -> None:
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self._alive = False
        self.name = name
        self.daemon = daemon

    def start(self) -> None:
        self._alive = True
        try:
            target = self._target
            if target is None or not callable(target):
                raise AssertionError("inline thread target must be callable")
            target(*self._args, **self._kwargs)
        finally:
            self._alive = False

    def join(self, timeout: float | None = None) -> None:
        _ = timeout

    def is_alive(self) -> bool:
        return self._alive


@pytest.fixture
def cleanup_config(tmp_path: Path) -> CleanupConfig:
    media_root = tmp_path / "backing"
    history_db_path = tmp_path / "state" / "cleanup_history.db"
    return CleanupConfig(
        history_db_path=history_db_path,
        media_root=media_root,
        report_limit=5,
    )


@pytest.fixture
def mapping_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "mapping.sqlite3"


@pytest.fixture
def mapping_access(mapping_db_path: Path) -> FakeMappingAccess:
    return FakeMappingAccess(mapping_db_path)


@pytest.fixture
def retention_source() -> FakeRetentionSource:
    return FakeRetentionSource(RetentionPolicy())


@pytest.fixture
def cleanup_service(
    cleanup_config: CleanupConfig,
    retention_source: FakeRetentionSource,
    mapping_access: FakeMappingAccess,
) -> CleanupService:
    return CleanupService(
        config=cleanup_config,
        retention_source=retention_source,
        mapping_access=mapping_access,
    )


@pytest.fixture
def inline_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teslausb_web.services.cleanup.service.threading.Thread",
        InlineThread,
    )


def _init_mapping_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS indexed_files (file_path TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS waypoints (video_path TEXT)")
        connection.commit()
    finally:
        connection.close()


def _insert_indexed(db_path: Path, *paths: str) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executemany(
            "INSERT INTO indexed_files (file_path) VALUES (?)",
            [(path,) for path in paths],
        )
        connection.commit()
    finally:
        connection.close()


def _insert_waypoints(db_path: Path, *paths: str) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executemany(
            "INSERT INTO waypoints (video_path) VALUES (?)",
            [(path,) for path in paths],
        )
        connection.commit()
    finally:
        connection.close()


def _category_root(config: CleanupConfig, category: str) -> Path:
    return config.media_root / _CATEGORY_FOLDERS[category]


def _set_age(path: Path, *, days: int = 0, hours: int = 0, seconds: int = 0) -> None:
    timestamp = (
        datetime.now(tz=UTC) - timedelta(days=days, hours=hours, seconds=seconds)
    ).timestamp()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    path.write_bytes(b"x" * 1024)
    path.chmod(0o666)
    Path(path).touch()
    path.write_bytes(path.read_bytes())
    path_ts = float(timestamp)
    import os

    os.utime(path, (path_ts, path_ts))


def _write_group(  # noqa: PLR0913
    config: CleanupConfig,
    category: str,
    stem: str,
    *,
    cameras: tuple[str, ...] = ("front",),
    age_days: int = 40,
    age_hours: int = 0,
    size_bytes: int = 1024,
    subdir: str = "",
) -> tuple[Path, ...]:
    base = _category_root(config, category)
    if subdir:
        base = base / subdir
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for camera in cameras:
        clip = base / f"{stem}-{camera}.mp4"
        clip.write_bytes(b"x" * size_bytes)
        timestamp = (datetime.now(tz=UTC) - timedelta(days=age_days, hours=age_hours)).timestamp()
        import os

        os.utime(clip, (timestamp, timestamp))
        written.append(clip)
    return tuple(written)


def _relative_path(config: CleanupConfig, category: str, path: Path) -> str:
    return path.relative_to(config.media_root).as_posix()


def _make_group_with_db_entry(  # noqa: PLR0913
    config: CleanupConfig,
    db_path: Path,
    category: str,
    stem: str,
    *,
    indexed: bool = True,
    gps: bool = False,
    age_days: int = 40,
) -> str:
    files = _write_group(config, category, stem, cameras=("front", "back"), age_days=age_days)
    front_relative = _relative_path(config, category, files[0])
    if indexed:
        _insert_indexed(db_path, front_relative)
    if gps:
        _insert_waypoints(db_path, front_relative)
    return front_relative


def _disk_usage(total: int, used: int, free: int) -> object:
    return type("Usage", (), {"total": total, "used": used, "free": free})()


def _replace_numeric_field_with_zero(
    cleanup_config: CleanupConfig,
    field_name: str,
) -> CleanupConfig:
    if field_name == "max_concurrent_runs":
        return replace(cleanup_config, max_concurrent_runs=0)
    if field_name == "orphan_scan_batch_size":
        return replace(cleanup_config, orphan_scan_batch_size=0)
    if field_name == "sample_path_limit":
        return replace(cleanup_config, sample_path_limit=0)
    if field_name == "recent_protection_hours":
        return replace(cleanup_config, recent_protection_hours=0)
    if field_name == "orphan_min_age_seconds":
        return replace(cleanup_config, orphan_min_age_seconds=0)
    return replace(cleanup_config, report_limit=0)


class TestCleanupConfigValidation:
    def test_rejects_relative_history_path(self, tmp_path: Path) -> None:
        with pytest.raises(CleanupConfigError, match="history_db_path"):
            CleanupConfig(
                history_db_path=Path("relative.db"),
                media_root=tmp_path / "media",
            )

    @pytest.mark.parametrize(
        "field_name",
        [
            "max_concurrent_runs",
            "orphan_scan_batch_size",
            "sample_path_limit",
            "recent_protection_hours",
            "orphan_min_age_seconds",
            "report_limit",
        ],
    )
    def test_rejects_non_positive_numeric_fields(
        self, cleanup_config: CleanupConfig, field_name: str
    ) -> None:
        with pytest.raises(CleanupConfigError, match=field_name):
            _replace_numeric_field_with_zero(cleanup_config, field_name)



def test_load_mapping_snapshot_reads_gps_and_index_rows(
    mapping_db_path: Path, mapping_access: FakeMappingAccess
) -> None:
    _insert_indexed(mapping_db_path, "RecentClips/a-front.mp4", "RecentClips/b-front.mp4")
    _insert_waypoints(mapping_db_path, "RecentClips/a-front.mp4")
    snapshot = load_mapping_snapshot(mapping_access.open_db)
    assert snapshot.indexed_paths == ("RecentClips/a-front.mp4", "RecentClips/b-front.mp4")
    assert "RecentClips/a-front.mp4" in snapshot.gps_relative_paths
    assert snapshot.indexed_canonical_keys


def test_discover_clip_groups_marks_gps_and_groups_cameras(
    cleanup_config: CleanupConfig,
    mapping_db_path: Path,
    mapping_access: FakeMappingAccess,
) -> None:
    front_relative = _make_group_with_db_entry(
        cleanup_config,
        mapping_db_path,
        "recent",
        "2024-01-01_00-00",
        gps=True,
    )
    snapshot = load_mapping_snapshot(mapping_access.open_db)
    groups = discover_clip_groups(cleanup_config, snapshot)
    assert len(groups) == 1
    assert groups[0].display_path == front_relative
    assert len(groups[0].files) == 2
    assert groups[0].has_gps is True


def test_discover_clip_groups_ignores_non_mp4(cleanup_config: CleanupConfig) -> None:
    root = _category_root(cleanup_config, "recent")
    root.mkdir(parents=True, exist_ok=True)
    (root / "skip.txt").write_text("skip", encoding="utf-8")
    snapshot = MappingSnapshot(
        gps_relative_paths=frozenset(), indexed_paths=(), indexed_canonical_keys=frozenset()
    )
    assert discover_clip_groups(cleanup_config, snapshot) == ()


def test_scan_orphans_detects_fs_only_and_db_only(
    cleanup_config: CleanupConfig,
    mapping_db_path: Path,
    mapping_access: FakeMappingAccess,
) -> None:
    _make_group_with_db_entry(cleanup_config, mapping_db_path, "recent", "indexed", indexed=True)
    _make_group_with_db_entry(cleanup_config, mapping_db_path, "recent", "fs-only", indexed=False)
    _insert_indexed(mapping_db_path, "RecentClips/db-only-front.mp4")
    snapshot = load_mapping_snapshot(mapping_access.open_db)
    groups = discover_clip_groups(cleanup_config, snapshot)
    orphan_details = scan_orphans(cleanup_config, snapshot, groups)
    assert orphan_details.scan.fs_only_paths == ("RecentClips/fs-only-front.mp4",)
    assert orphan_details.scan.db_only_paths == (str(Path("RecentClips/db-only-front.mp4")),)
    assert orphan_details.scan.total_bytes_recoverable > 0


def test_scan_orphans_ignores_recent_groups(
    cleanup_config: CleanupConfig,
    mapping_db_path: Path,
    mapping_access: FakeMappingAccess,
) -> None:
    _make_group_with_db_entry(
        cleanup_config,
        mapping_db_path,
        "recent",
        "too-new",
        indexed=False,
        age_days=0,
    )
    snapshot = load_mapping_snapshot(mapping_access.open_db)
    groups = discover_clip_groups(cleanup_config, snapshot)
    orphan_details = scan_orphans(cleanup_config, snapshot, groups)
    assert orphan_details.scan.fs_only_paths == ()


@pytest.mark.parametrize(
    ("category", "policy_attr"),
    [
        ("recent", "keep_recent_clips"),
        ("saved", "keep_saved_clips"),
        ("event", "keep_event_clips"),
        ("encrypted", "keep_encrypted_clips"),
    ],
)
def test_build_preview_plan_respects_keep_flags(
    cleanup_config: CleanupConfig,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    policy_attr: str,
) -> None:
    monkeypatch.setattr(
        "teslausb_web.services.cleanup.preview.shutil.disk_usage",
        lambda _probe: _disk_usage(1_000, 900, 100),
    )
    _write_group(cleanup_config, category, "keep-me", age_days=40)
    snapshot = MappingSnapshot(
        gps_relative_paths=frozenset(), indexed_paths=(), indexed_canonical_keys=frozenset()
    )
    groups = discover_clip_groups(cleanup_config, snapshot)
    plan = build_preview_plan(
        cleanup_config, replace(RetentionPolicy(), **{policy_attr: True}), groups
    )
    assert plan.candidate_count == 0
    assert plan.protected_count == 1


def test_build_preview_plan_protects_recent_write(
    cleanup_config: CleanupConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "teslausb_web.services.cleanup.preview.shutil.disk_usage",
        lambda _probe: _disk_usage(1_000, 900, 100),
    )
    _write_group(cleanup_config, "recent", "new", age_days=0, age_hours=0)
    snapshot = MappingSnapshot(
        gps_relative_paths=frozenset(), indexed_paths=(), indexed_canonical_keys=frozenset()
    )
    groups = discover_clip_groups(cleanup_config, snapshot)
    plan = build_preview_plan(cleanup_config, RetentionPolicy(), groups)
    assert plan.candidate_count == 0
    assert plan.protected_count == 1


def test_build_preview_plan_protects_gps_by_default(
    cleanup_config: CleanupConfig,
    mapping_db_path: Path,
    mapping_access: FakeMappingAccess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "teslausb_web.services.cleanup.preview.shutil.disk_usage",
        lambda _probe: _disk_usage(1_000, 900, 100),
    )
    _make_group_with_db_entry(cleanup_config, mapping_db_path, "recent", "gps", gps=True)
    snapshot = load_mapping_snapshot(mapping_access.open_db)
    groups = discover_clip_groups(cleanup_config, snapshot)
    plan = build_preview_plan(
        cleanup_config, replace(RetentionPolicy(), keep_recent_clips=False), groups
    )
    assert plan.candidate_count == 0
    assert plan.protected_count == 1


def test_build_preview_plan_can_delete_gps_when_enabled(
    cleanup_config: CleanupConfig,
    mapping_db_path: Path,
    mapping_access: FakeMappingAccess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "teslausb_web.services.cleanup.preview.shutil.disk_usage",
        lambda _probe: _disk_usage(1_000, 900, 100),
    )
    gps_config = replace(cleanup_config, delete_gps_tagged_clips=True)
    _make_group_with_db_entry(gps_config, mapping_db_path, "recent", "gps", gps=True)
    snapshot = load_mapping_snapshot(mapping_access.open_db)
    groups = discover_clip_groups(gps_config, snapshot)
    plan = build_preview_plan(
        gps_config, replace(RetentionPolicy(), keep_recent_clips=False), groups
    )
    assert plan.candidate_count == 1


def test_build_preview_plan_selects_for_free_space_target(
    cleanup_config: CleanupConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "teslausb_web.services.cleanup.preview.shutil.disk_usage",
        lambda _probe: _disk_usage(1_000, 960, 40),
    )
    _write_group(cleanup_config, "recent", "old-a", age_days=5, size_bytes=100)
    _write_group(cleanup_config, "recent", "old-b", age_days=4, size_bytes=100)
    snapshot = MappingSnapshot(
        gps_relative_paths=frozenset(), indexed_paths=(), indexed_canonical_keys=frozenset()
    )
    groups = discover_clip_groups(cleanup_config, snapshot)
    plan = build_preview_plan(
        cleanup_config,
        replace(RetentionPolicy(), max_age_days=365, target_free_pct=30, keep_recent_clips=False),
        groups,
    )
    assert plan.candidate_count == 2
    assert plan.projected_free_pct > plan.current_free_pct



def test_execute_groups_dry_run_leaves_files(cleanup_config: CleanupConfig) -> None:
    files = _write_group(cleanup_config, "recent", "dry-run", age_days=40)
    snapshot = MappingSnapshot(
        gps_relative_paths=frozenset(), indexed_paths=(), indexed_canonical_keys=frozenset()
    )
    group = discover_clip_groups(cleanup_config, snapshot)[0]
    result = execute_groups(
        cleanup_config,
        (group,),
        dry_run=True,
        cancel_event=threading.Event(),
    )
    assert result.deleted_count == 1
    assert files[0].exists()


def test_execute_groups_deletes_files_and_prunes_empty_parents(
    cleanup_config: CleanupConfig,
) -> None:
    files = _write_group(cleanup_config, "recent", "delete-me", age_days=40, subdir="trip")
    snapshot = MappingSnapshot(
        gps_relative_paths=frozenset(), indexed_paths=(), indexed_canonical_keys=frozenset()
    )
    group = discover_clip_groups(cleanup_config, snapshot)[0]
    result = execute_groups(
        cleanup_config,
        (group,),
        dry_run=False,
        cancel_event=threading.Event(),
    )
    assert result.deleted_count == 1
    assert not files[0].exists()
    assert not files[0].parent.exists()


def test_execute_groups_refuses_paths_outside_allowed_roots(
    cleanup_config: CleanupConfig, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    from teslausb_web.services.cleanup.discovery import ClipFile, ClipGroup

    modified_at = datetime.now(tz=UTC) - timedelta(days=40)
    group = ClipGroup(
        category="recent",
        folder_name="RecentClips",
        recording_key="outside",
        display_path="RecentClips/outside-front.mp4",
        files=(
            ClipFile(
                path=outside,
                relative_path="RecentClips/outside-front.mp4",
                size_bytes=1,
                modified_at=modified_at,
            ),
        ),
        total_bytes=1,
        oldest_modified_at=modified_at,
        newest_modified_at=modified_at,
        has_gps=False,
        front_relative_path="RecentClips/outside-front.mp4",
    )
    with pytest.raises(CleanupError, match="outside configured TeslaCam roots"):
        execute_groups(cleanup_config, (group,), dry_run=False, cancel_event=threading.Event())


def test_execute_groups_honors_cancel_event(cleanup_config: CleanupConfig) -> None:
    _write_group(cleanup_config, "recent", "cancel", age_days=40)
    snapshot = MappingSnapshot(
        gps_relative_paths=frozenset(), indexed_paths=(), indexed_canonical_keys=frozenset()
    )
    group = discover_clip_groups(cleanup_config, snapshot)[0]
    cancel_event = threading.Event()
    cancel_event.set()
    with pytest.raises(CleanupCancelledError):
        execute_groups(cleanup_config, (group,), dry_run=False, cancel_event=cancel_event)


def test_report_round_trip(cleanup_config: CleanupConfig) -> None:
    started_at = datetime.now(tz=UTC).isoformat()
    generated_at = datetime.now(tz=UTC).isoformat()
    start_run_record(
        cleanup_config,
        run_id="run-123",
        action="cleanup",
        dry_run=True,
        started_at=started_at,
        generated_at=generated_at,
        policy_snapshot={"max_age_days": 30},
        counts_by_category={"recent": 1},
        sample_paths=("RecentClips/a-front.mp4",),
        total_candidates=1,
    )
    record = load_run_record(cleanup_config, "run-123")
    assert record is not None
    finish_run_record(
        cleanup_config,
        replace(record, status="completed", deleted_count=1, deleted_bytes=1024),
    )
    recent = load_recent_run_records(cleanup_config, 5)
    assert recent[0].deleted_count == 1
    assert recent[0].status == "completed"


def test_cleanup_service_preview_summary_reports_available(
    cleanup_config: CleanupConfig,
    mapping_access: FakeMappingAccess,
    retention_source: FakeRetentionSource,
) -> None:
    service = CleanupService(
        config=cleanup_config,
        retention_source=retention_source,
        mapping_access=mapping_access,
    )
    summary = service.preview_summary()
    assert summary.preview_available is True
    assert summary.deferred_reason == ""


def test_cleanup_service_preview_includes_orphans(
    cleanup_service: CleanupService,
    cleanup_config: CleanupConfig,
    mapping_db_path: Path,
) -> None:
    _make_group_with_db_entry(cleanup_config, mapping_db_path, "recent", "orphan", indexed=False)
    preview = cleanup_service.preview()
    assert preview.orphan_scan is not None
    assert preview.orphan_scan.fs_only_paths == ("RecentClips/orphan-front.mp4",)


def test_start_execute_uses_default_dry_run(
    cleanup_service: CleanupService,
    cleanup_config: CleanupConfig,
    inline_threads: None,
) -> None:
    _write_group(cleanup_config, "recent", "default-dry", age_days=40)
    run_id = cleanup_service.start_execute(dry_run=None)
    run = cleanup_service.get_run_status(run_id).run
    assert run.dry_run is True
    assert run.status == "completed"


def test_start_execute_persists_completed_run(
    cleanup_service: CleanupService,
    cleanup_config: CleanupConfig,
    inline_threads: None,
) -> None:
    _write_group(cleanup_config, "recent", "persisted", age_days=40)
    run_id = cleanup_service.start_execute(dry_run=False)
    report = cleanup_service.report()
    assert report.recent_runs[0].run_id == run_id
    assert report.recent_runs[0].deleted_count == 1
    assert not (_category_root(cleanup_config, "recent") / "persisted-front.mp4").exists()


def test_start_execute_with_no_candidates_completes_immediately(
    cleanup_service: CleanupService,
    cleanup_config: CleanupConfig,
) -> None:
    _write_group(cleanup_config, "saved", "kept", age_days=40)
    run_id = cleanup_service.start_execute(dry_run=True)
    run = cleanup_service.get_run_status(run_id).run
    assert run.status == "completed"
    assert run.total_candidates == 0


def test_start_execute_blocks_concurrent_runs(
    cleanup_service: CleanupService,
    cleanup_config: CleanupConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_group(cleanup_config, "recent", "blocking", age_days=40)
    started = threading.Event()
    release = threading.Event()

    def _blocking_execute(*args: object, **kwargs: object) -> DeletionResult:
        _ = args, kwargs
        started.set()
        release.wait(timeout=5)
        return DeletionResult(deleted_count=0, deleted_bytes=0, deleted_paths=(), errors=())

    monkeypatch.setattr("teslausb_web.services.cleanup.service.execute_groups", _blocking_execute)
    run_id = cleanup_service.start_execute(dry_run=True)
    assert started.wait(timeout=5)
    with pytest.raises(CleanupError, match="already in progress"):
        cleanup_service.start_execute(dry_run=True)
    release.set()
    for _ in range(50):
        if not cleanup_service.get_run_status(run_id).active:
            break
        time.sleep(0.01)


def test_get_run_status_raises_for_unknown_run(cleanup_service: CleanupService) -> None:
    with pytest.raises(CleanupError, match="Unknown cleanup run"):
        cleanup_service.get_run_status("run-missing")


def test_purge_orphans_deletes_files_and_reindexes(
    cleanup_service: CleanupService,
    cleanup_config: CleanupConfig,
    mapping_access: FakeMappingAccess,
    mapping_db_path: Path,
) -> None:
    _make_group_with_db_entry(cleanup_config, mapping_db_path, "recent", "fs-only", indexed=False)
    _insert_indexed(mapping_db_path, "RecentClips/db-only-front.mp4")
    run = cleanup_service.purge_orphans()
    assert run.action == "orphan-purge"
    assert run.deleted_count == 2
    assert mapping_access.purge_calls == [(str(Path("RecentClips/db-only-front.mp4")),)]
    assert mapping_access.stale_calls == [("cleanup-orphan-purge", 0.0)]


def test_purge_orphans_records_mapping_purge_errors(
    cleanup_service: CleanupService,
    cleanup_config: CleanupConfig,
    mapping_access: FakeMappingAccess,
    mapping_db_path: Path,
) -> None:
    _insert_indexed(mapping_db_path, "RecentClips/db-only-front.mp4")
    mapping_access.raise_purge = RuntimeError("boom")
    run = cleanup_service.purge_orphans()
    assert run.status == "failed"
    assert any("dangling mapping rows" in error for error in run.errors)


def test_shutdown_cancels_active_thread(
    cleanup_service: CleanupService,
    cleanup_config: CleanupConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_group(cleanup_config, "recent", "cancelled", age_days=40)
    started = threading.Event()

    def _blocking_execute(*args: object, **kwargs: object) -> DeletionResult:
        _ = args
        cancel_event = kwargs["cancel_event"]
        assert isinstance(cancel_event, threading.Event)
        started.set()
        while not cancel_event.is_set():
            time.sleep(0.01)
        raise CleanupCancelledError("cleanup run cancelled")

    monkeypatch.setattr("teslausb_web.services.cleanup.service.execute_groups", _blocking_execute)
    run_id = cleanup_service.start_execute(dry_run=False)
    assert started.wait(timeout=5)
    assert cleanup_service.shutdown(timeout=1.0) is True
    run_status = cleanup_service.get_run_status(run_id)
    assert run_status.run.status == "cancelled"


def test_make_cleanup_service_accepts_direct_config(
    cleanup_config: CleanupConfig,
    retention_source: FakeRetentionSource,
    mapping_access: FakeMappingAccess,
) -> None:
    built = make_cleanup_service(cleanup_config, retention_source, mapping_access, None)
    assert built.config.history_db_path == cleanup_config.history_db_path


def test_make_cleanup_service_builds_from_web_config(
    tmp_path: Path, mapping_access: FakeMappingAccess
) -> None:
    state_dir = tmp_path / "state"
    cfg = WebConfig(
        web=WebSection(secret_key="x" * 32),
        paths=PathsSection(state_dir=state_dir, backing_root=tmp_path / "backing"),
        storage_retention=StorageRetentionSection(policy_path=state_dir / "retention_policy.json"),
        cleanup=CleanupSection(history_db_path=state_dir / "cleanup_history.db", report_limit=7),
        mapping=MappingSection(
            db_path=state_dir / "mapping.db",
            backup_dir=state_dir / "mapping-backups",
            media_root=tmp_path / "backing",
        ),
        source_path=None,
    )
    retention_service = make_storage_retention_service(cfg)
    built = make_cleanup_service(cfg, retention_service, mapping_access, None)
    assert built.config.history_db_path == state_dir / "cleanup_history.db"
    assert built.config.report_limit == 7


def test_policy_snapshot_contains_expected_keys() -> None:
    snapshot = _policy_snapshot(RetentionPolicy())
    assert snapshot["max_age_days"] == 30
    assert snapshot["keep_saved_clips"] is True
    assert snapshot["archived_clips_days"] == 30
