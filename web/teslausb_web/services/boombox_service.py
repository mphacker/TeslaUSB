"""B-1 service: Tesla Boombox listing, upload, and deletion."""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

    from werkzeug.datastructures import FileStorage

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_COPY_CHUNK_BYTES: Final[int] = 65_536
_PATH_TRAVERSAL_FORBIDDEN: Final[frozenset[str]] = frozenset({"/", "\\", "..", "\x00"})


class BoomboxError(ValueError):
    """The requested boombox operation is invalid."""


class BoomboxFileError(OSError):
    """A boombox file or directory could not be read or written."""


class _SizeLimitExceeded(ValueError):
    def __init__(self, size_bytes: int) -> None:
        super().__init__(str(size_bytes))
        self.size_bytes = size_bytes


@dataclass(frozen=True, slots=True)
class BoomboxConfig:
    base_dir: Path
    max_file_bytes: int
    max_files: int
    allowed_extensions: tuple[str, ...]
    schedule_cache_invalidation: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class BoomboxFile:
    filename: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class BoomboxListing:
    files: tuple[BoomboxFile, ...]
    max_files: int


@dataclass(frozen=True, slots=True)
class UploadResult:
    success: bool
    message: str
    file_count: int


@dataclass(frozen=True, slots=True)
class DeleteResult:
    success: bool
    message: str
    deleted_count: int


class BoomboxService:
    """Manage Tesla Boombox audio clips stored beneath the configured Music root."""

    def __init__(self, config: BoomboxConfig) -> None:
        self._config = config
        self._base_dir = config.base_dir
        self._allowed_extensions = tuple(
            extension.lower() for extension in config.allowed_extensions
        )
        self._lock = threading.RLock()

    def list_files(self) -> BoomboxListing:
        """List boombox files sorted alphabetically for Tesla's first-N selection rule."""
        with self._lock:
            if not self._base_dir.exists():
                return BoomboxListing(files=(), max_files=self._config.max_files)
            try:
                files: list[BoomboxFile] = []
                for candidate in sorted(
                    self._base_dir.iterdir(),
                    key=lambda path: path.name.lower(),
                ):
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    if candidate.suffix.lower() not in self._allowed_extensions:
                        continue
                    stats = candidate.stat()
                    files.append(
                        BoomboxFile(
                            filename=candidate.name,
                            size_bytes=stats.st_size,
                            modified_at=datetime.fromtimestamp(stats.st_mtime, tz=UTC),
                        )
                    )
            except OSError as exc:
                raise BoomboxFileError(f"Failed to list boombox files: {exc}") from exc
            return BoomboxListing(files=tuple(files), max_files=self._config.max_files)

    def upload_file(self, uploaded_file: FileStorage) -> UploadResult:
        """Validate and atomically publish one boombox clip."""
        with self._lock:
            if not uploaded_file.filename:
                return UploadResult(success=False, message="No file selected", file_count=0)
            try:
                safe_name = _validate_boombox_filename(
                    uploaded_file.filename,
                    allowed_extensions=self._allowed_extensions,
                )
                size_bytes = _write_stream_atomically(
                    source=_rewind_stream(uploaded_file),
                    destination=self._base_dir / safe_name,
                    max_size=self._config.max_file_bytes,
                )
            except _SizeLimitExceeded as exc:
                return UploadResult(
                    success=False,
                    message=_size_limit_message(exc.size_bytes, self._config.max_file_bytes),
                    file_count=0,
                )
            except BoomboxError as exc:
                return UploadResult(success=False, message=str(exc), file_count=0)
            logger.info("Uploaded boombox file %s (%s bytes)", safe_name, size_bytes)
            _schedule_cache_invalidation(self._config.schedule_cache_invalidation)
            return UploadResult(success=True, message=f"Uploaded {safe_name}", file_count=1)

    def delete_file(self, filename: str) -> DeleteResult:
        """Delete one boombox clip by filename."""
        with self._lock:
            safe_name = _validate_boombox_filename(
                filename,
                allowed_extensions=self._allowed_extensions,
            )
            candidate = self._base_dir / safe_name
            if candidate.is_symlink() or not candidate.is_file():
                return DeleteResult(success=False, message="File not found", deleted_count=0)
            try:
                candidate.unlink()
            except OSError as exc:
                raise BoomboxFileError(f"Failed to delete boombox file {safe_name}: {exc}") from exc
            _safe_fsync_dir(self._base_dir)
            logger.info("Deleted boombox file %s", safe_name)
            _schedule_cache_invalidation(self._config.schedule_cache_invalidation)
            return DeleteResult(success=True, message=f"Deleted {safe_name}", deleted_count=1)


def _schedule_cache_invalidation(schedule: Callable[[], None] | None) -> None:
    if schedule is not None:
        schedule()


def _validate_boombox_filename(name: str | None, *, allowed_extensions: tuple[str, ...]) -> str:
    if name is None:
        raise BoomboxError("Filename is required")
    normalized = name.strip()
    if not normalized:
        raise BoomboxError("Filename is required")
    if any(token in normalized for token in _PATH_TRAVERSAL_FORBIDDEN):
        raise BoomboxError(f"Invalid filename: {name!r}")
    candidate = Path(normalized).name
    if candidate != normalized or candidate in {".", ".."}:
        raise BoomboxError(f"Invalid filename: {name!r}")
    if Path(candidate).suffix.lower() not in allowed_extensions:
        raise BoomboxError("Only MP3 and WAV files are allowed")
    return candidate


def _rewind_stream(file_storage: FileStorage) -> IO[bytes]:
    with contextlib.suppress(OSError, ValueError):
        file_storage.stream.seek(0)
    return file_storage.stream


def _write_stream_atomically(*, source: IO[bytes], destination: Path, max_size: int) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as file_handle:
            size_bytes = _copy_stream_with_limit(source, file_handle, max_size=max_size)
            file_handle.flush()
            _safe_fsync(file_handle.fileno())
        os.replace(temp_path, destination)  # noqa: PTH105 - atomic publish contract
        _safe_fsync_dir(destination.parent)
    except _SizeLimitExceeded:
        _safe_unlink(temp_path)
        raise
    except OSError as exc:
        _safe_unlink(temp_path)
        raise BoomboxFileError(f"Failed to write {destination}: {exc}") from exc
    return size_bytes


def _copy_stream_with_limit(source: IO[bytes], destination: IO[bytes], *, max_size: int) -> int:
    size_bytes = 0
    while True:
        chunk = source.read(_COPY_CHUNK_BYTES)
        if not chunk:
            return size_bytes
        size_bytes += len(chunk)
        if size_bytes > max_size:
            raise _SizeLimitExceeded(size_bytes)
        destination.write(chunk)


def _safe_fsync(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.fsync(fd)


def _safe_fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        _safe_fsync(fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


def _size_limit_message(size_bytes: int, limit_bytes: int) -> str:
    return f"File is {size_bytes / 1_048_576:.2f} MiB. Limit is {limit_bytes / 1_048_576:.2f} MiB."


def make_boombox_service(
    cfg: WebConfig,
    *,
    schedule_cache_invalidation: Callable[[], None] | None = None,
) -> BoomboxService:
    """Build a boombox service rooted at the configured Tesla Music/Boombox folder."""
    return BoomboxService(
        BoomboxConfig(
            base_dir=cfg.paths.backing_root / cfg.music.folder / cfg.boombox.base_dir,
            max_file_bytes=cfg.boombox.max_file_bytes,
            max_files=cfg.boombox.max_files,
            allowed_extensions=cfg.boombox.allowed_extensions,
            schedule_cache_invalidation=schedule_cache_invalidation,
        )
    )


__all__ = (
    "BoomboxConfig",
    "BoomboxError",
    "BoomboxFile",
    "BoomboxFileError",
    "BoomboxListing",
    "BoomboxService",
    "DeleteResult",
    "UploadResult",
    "make_boombox_service",
)
