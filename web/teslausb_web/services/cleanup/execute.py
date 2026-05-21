from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teslausb_web.services.cleanup.discovery import ClipGroup

if TYPE_CHECKING:
    import threading
    from pathlib import Path

    from teslausb_web.services.cleanup.service import CleanupConfig


@dataclass(frozen=True, slots=True)
class DeletionResult:
    deleted_count: int
    deleted_bytes: int
    deleted_paths: tuple[str, ...]
    errors: tuple[str, ...]


ProgressCallback = Callable[[ClipGroup, int, int, int, tuple[str, ...]], None]


def execute_groups(
    config: CleanupConfig,
    groups: tuple[ClipGroup, ...],
    *,
    dry_run: bool,
    cancel_event: threading.Event,
    progress: ProgressCallback | None = None,
) -> DeletionResult:
    from teslausb_web.services.cleanup.service import CleanupCancelledError  # noqa: PLC0415

    deleted_count = 0
    deleted_bytes = 0
    deleted_paths: list[str] = []
    errors: list[str] = []
    allowed_roots = _allowed_roots(config)
    for processed, group in enumerate(groups, start=1):
        if cancel_event.is_set():
            raise CleanupCancelledError("cleanup run cancelled")
        group_failed = False
        for clip_file in group.files:
            validated_path = _validated_path(clip_file.path, allowed_roots)
            if dry_run:
                continue
            try:
                validated_path.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"Failed to delete {group.display_path}: {exc}")
                group_failed = True
                continue
            deleted_paths.append(str(validated_path))
        if not group_failed:
            deleted_count += 1
            deleted_bytes += group.total_bytes
            if not dry_run:
                _prune_empty_parents(group, allowed_roots)
        if progress is not None:
            progress(group, processed, deleted_count, deleted_bytes, tuple(errors))
        if cancel_event.is_set():
            raise CleanupCancelledError("cleanup run cancelled")
    return DeletionResult(
        deleted_count=deleted_count,
        deleted_bytes=deleted_bytes,
        deleted_paths=tuple(deleted_paths),
        errors=tuple(errors),
    )


def _allowed_roots(config: CleanupConfig) -> tuple[Path, ...]:
    roots = {config.media_root.resolve(), config.archive_root.resolve()}
    return tuple(sorted(roots, key=str))


def _validated_path(path: Path, allowed_roots: tuple[Path, ...]) -> Path:
    from teslausb_web.services.cleanup.service import CleanupError  # noqa: PLC0415

    resolved = path.resolve()
    for root in allowed_roots:
        with suppress(ValueError):
            resolved.relative_to(root)
            return resolved
    raise CleanupError(f"Refusing to delete path outside configured TeslaCam roots: {resolved}")


def _prune_empty_parents(group: ClipGroup, allowed_roots: tuple[Path, ...]) -> None:
    roots = set(allowed_roots)
    for clip_file in group.files:
        current = clip_file.path.resolve().parent
        while current not in roots:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
