"""B-1 service: Tesla music library listing, upload, deletion, and folder management."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

    from werkzeug.datastructures import FileStorage

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_COPY_CHUNK_BYTES: Final[int] = 65_536
_PATH_TRAVERSAL_FORBIDDEN: Final[frozenset[str]] = frozenset({"/", "\\", "..", "\x00"})
_SANITIZE_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f/:\\]")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_UPLOAD_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")
_UPLOAD_DIRNAME: Final[str] = ".uploads"


class MusicError(ValueError):
    """The requested music operation is invalid."""


class MusicFileError(OSError):
    """A music file or directory could not be read or written."""


class _SizeLimitExceeded(ValueError):
    def __init__(self, size_bytes: int) -> None:
        super().__init__(str(size_bytes))
        self.size_bytes = size_bytes


@dataclass(frozen=True, slots=True)
class MusicDirectory:
    name: str
    path: str


@dataclass(frozen=True, slots=True)
class MusicFile:
    name: str
    path: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class MusicListing:
    directories: tuple[MusicDirectory, ...]
    files: tuple[MusicFile, ...]
    relative_path: str
    used_bytes: int
    free_bytes: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class UploadResult:
    success: bool
    message: str
    file_count: int


@dataclass(frozen=True, slots=True)
class ChunkResult:
    success: bool
    message: str
    is_finalized: bool


@dataclass(frozen=True, slots=True)
class DeleteResult:
    success: bool
    message: str
    deleted_count: int


@dataclass(frozen=True, slots=True)
class DirectoryResult:
    success: bool
    message: str


@dataclass(frozen=True, slots=True)
class MoveResult:
    success: bool
    message: str
    destination_path: str | None


class MusicService:
    """Manage Tesla music files stored beneath the configured backing root."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        storage_root: Path,
        music_folder: Path,
        max_file_size: int,
        chunk_size: int,
        free_space_reserve: int,
        stale_chunk_age: int,
        allowed_extensions: tuple[str, ...],
    ) -> None:
        self._storage_root = storage_root
        self._music_folder = music_folder
        self._max_file_size = max_file_size
        self._chunk_size = chunk_size
        self._free_space_reserve = free_space_reserve
        self._stale_chunk_age = stale_chunk_age
        self._allowed_extensions = tuple(extension.lower() for extension in allowed_extensions)
        self._lock = threading.RLock()

    def resolve_file_path(self, rel_path: str) -> Path:
        """Return an absolute path for an existing music file."""
        with self._lock:
            candidate, _ = self._resolve_file_candidate(rel_path)
            if candidate.is_symlink() or not candidate.is_file():
                raise MusicError("File not found")
            return candidate

    def list_files(self, rel_path: str = "") -> MusicListing:
        """List folders and music files inside ``rel_path``."""
        with self._lock:
            current_rel = _normalize_rel_path(rel_path)
            self._music_folder.mkdir(parents=True, exist_ok=True)
            target_dir = self._resolve_directory(current_rel)
            if target_dir.is_symlink() or not target_dir.is_dir():
                raise MusicError("Folder not found")
            total_bytes, used_bytes, free_bytes = self._disk_usage()
            try:
                directories: list[MusicDirectory] = []
                files: list[MusicFile] = []
                for entry in sorted(target_dir.iterdir(), key=lambda path: path.name.lower()):
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    entry_rel = _join_relpath(current_rel, entry.name)
                    if entry.is_dir():
                        directories.append(MusicDirectory(name=entry.name, path=entry_rel))
                        continue
                    if not entry.is_file() or entry.suffix.lower() not in self._allowed_extensions:
                        continue
                    stats = entry.stat()
                    files.append(
                        MusicFile(
                            name=entry.name,
                            path=entry_rel,
                            size_bytes=stats.st_size,
                            modified_at=datetime.fromtimestamp(stats.st_mtime, tz=UTC),
                        )
                    )
            except OSError as exc:
                raise MusicFileError(f"Failed to list music files: {exc}") from exc
            return MusicListing(
                directories=tuple(directories),
                files=tuple(files),
                relative_path=current_rel,
                used_bytes=used_bytes,
                free_bytes=free_bytes,
                total_bytes=total_bytes,
            )

    def upload_files(
        self,
        uploaded_files: Iterable[FileStorage],
        rel_path: str = "",
    ) -> UploadResult:
        """Save one or more uploaded music files into the requested folder."""
        with self._lock:
            target_dir = self._resolve_directory(rel_path)
            candidates = tuple(file for file in uploaded_files if file.filename)
            if not candidates:
                return UploadResult(success=False, message="No files selected", file_count=0)
            target_dir.mkdir(parents=True, exist_ok=True)
            successes = 0
            errors: list[str] = []
            for uploaded_file in candidates:
                raw_name = uploaded_file.filename
                if raw_name is None:
                    continue
                try:
                    safe_name = _validate_music_filename(
                        raw_name,
                        allowed_extensions=self._allowed_extensions,
                    )
                    size_bytes = self._write_upload_atomically(
                        source=_rewind_stream(uploaded_file),
                        destination=target_dir / safe_name,
                    )
                except _SizeLimitExceeded as exc:
                    errors.append(
                        _size_limit_message(raw_name, exc.size_bytes, self._max_file_size)
                    )
                    continue
                except MusicError as exc:
                    errors.append(str(exc))
                    continue
                logger.info("Uploaded music file %s (%s bytes)", safe_name, size_bytes)
                successes += 1
            if successes == 0:
                if errors:
                    return UploadResult(success=False, message="; ".join(errors), file_count=0)
                return UploadResult(success=False, message="No files selected", file_count=0)
            if errors:
                return UploadResult(
                    success=True,
                    message=(
                        f"Successfully uploaded {successes} file(s). Errors: {'; '.join(errors)}"
                    ),
                    file_count=successes,
                )
            return UploadResult(
                success=True,
                message=f"Successfully uploaded {successes} file(s)",
                file_count=successes,
            )

    def save_file(self, uploaded_file: FileStorage, rel_path: str = "") -> UploadResult:
        """Compatibility wrapper for single-file uploads."""
        with self._lock:
            if not uploaded_file.filename:
                return UploadResult(success=False, message="No files selected", file_count=0)
            try:
                safe_name = _validate_music_filename(
                    uploaded_file.filename,
                    allowed_extensions=self._allowed_extensions,
                )
            except MusicError as exc:
                return UploadResult(success=False, message=str(exc), file_count=0)
            result = self.upload_files([uploaded_file], rel_path)
            if result.success and result.file_count == 1:
                return UploadResult(success=True, message=f"Uploaded {safe_name}", file_count=1)
            return result

    def handle_chunk(  # noqa: PLR0913
        self,
        upload_id: str,
        filename: str,
        chunk_index: int,
        total_chunks: int,
        total_size: int,
        stream: IO[bytes],
        rel_path: str = "",
    ) -> ChunkResult:
        """Append one chunk to a staged upload and atomically finalize the last chunk."""
        with self._lock:
            if total_size > self._max_file_size:
                raise MusicError(_size_limit_message(filename, total_size, self._max_file_size))
            if total_size < 0:
                raise MusicError("File size must be non-negative")
            if total_chunks <= 0:
                raise MusicError("total_chunks must be greater than zero")
            if chunk_index < 0 or chunk_index >= total_chunks:
                raise MusicError("chunk_index is out of range")
            if _UPLOAD_ID_RE.fullmatch(upload_id) is None:
                raise MusicError("Invalid upload ID")
            safe_name = _validate_music_filename(
                filename,
                allowed_extensions=self._allowed_extensions,
            )
            target_dir = self._resolve_directory(rel_path)
            target_dir.mkdir(parents=True, exist_ok=True)
            uploads_dir = target_dir / _UPLOAD_DIRNAME
            uploads_dir.mkdir(parents=True, exist_ok=True)
            staged_path = uploads_dir / f"{upload_id}.part"
            final_path = target_dir / safe_name
            if chunk_index == 0:
                _purge_stale_chunks(uploads_dir, stale_chunk_age=self._stale_chunk_age)
                _safe_unlink(staged_path)
                self._ensure_free_space(total_size)
            chunk_size = _append_chunk(
                stream,
                staged_path=staged_path,
                max_chunk_size=self._chunk_size,
            )
            logger.debug(
                "Stored music chunk %s/%s for %s (%s bytes)",
                chunk_index + 1,
                total_chunks,
                safe_name,
                chunk_size,
            )
            if chunk_index < total_chunks - 1:
                return ChunkResult(success=True, message="Chunk stored", is_finalized=False)
            try:
                actual_size = staged_path.stat().st_size
            except OSError as exc:
                raise MusicFileError(f"Failed to stat staged upload {staged_path}: {exc}") from exc
            if actual_size != total_size:
                _safe_unlink(staged_path)
                raise MusicError(f"Size mismatch. Expected {total_size} bytes, got {actual_size}")
            try:
                os.replace(staged_path, final_path)  # noqa: PTH105 - atomic publish contract
            except OSError as exc:
                _safe_unlink(staged_path)
                raise MusicFileError(f"Failed to finalize upload {final_path}: {exc}") from exc
            logger.info("Uploaded music file %s via chunked transfer", safe_name)
            return ChunkResult(success=True, message=f"Uploaded {safe_name}", is_finalized=True)

    def delete_file(self, rel_path: str) -> DeleteResult:
        """Delete one music file by relative path."""
        with self._lock:
            candidate, safe_rel = self._resolve_file_candidate(rel_path)
            if candidate.is_symlink() or not candidate.is_file():
                return DeleteResult(success=False, message="File not found", deleted_count=0)
            try:
                candidate.unlink()
            except OSError as exc:
                raise MusicFileError(f"Failed to delete music file {safe_rel}: {exc}") from exc
            logger.info("Deleted music file %s", safe_rel)
            return DeleteResult(success=True, message=f"Deleted {candidate.name}", deleted_count=1)

    def bulk_delete(self, rel_paths: Iterable[str]) -> DeleteResult:
        """Delete multiple music files and aggregate per-file failures."""
        with self._lock:
            normalized = tuple(
                dict.fromkeys(self._normalize_file_relpath(path) for path in rel_paths)
            )
            if not normalized:
                return DeleteResult(success=False, message="No files selected", deleted_count=0)
            deleted_count = 0
            errors: list[str] = []
            for safe_rel in normalized:
                try:
                    result = self.delete_file(safe_rel)
                except MusicFileError as exc:
                    errors.append(f"{safe_rel}: {exc}")
                    continue
                if result.success:
                    deleted_count += 1
                else:
                    errors.append(f"{safe_rel}: {result.message}")
            if deleted_count == 0:
                if errors:
                    return DeleteResult(success=False, message="; ".join(errors), deleted_count=0)
                return DeleteResult(success=False, message="No files selected", deleted_count=0)
            if errors:
                return DeleteResult(
                    success=True,
                    message=f"Deleted {deleted_count} file(s). Errors: {'; '.join(errors)}",
                    deleted_count=deleted_count,
                )
            return DeleteResult(
                success=True,
                message=f"Deleted {deleted_count} file(s)",
                deleted_count=deleted_count,
            )

    def create_directory(self, rel_path: str, name: str) -> DirectoryResult:
        """Create one child directory inside ``rel_path``."""
        with self._lock:
            safe_name = _secure_filename(name)
            base_dir = self._resolve_directory(rel_path)
            base_dir.mkdir(parents=True, exist_ok=True)
            target_dir = base_dir / safe_name
            try:
                target_dir.mkdir()
            except FileExistsError:
                return DirectoryResult(success=False, message="Folder already exists")
            except OSError as exc:
                raise MusicFileError(f"Failed to create directory {safe_name}: {exc}") from exc
            logger.info(
                "Created music directory %s",
                _join_relpath(_normalize_rel_path(rel_path), safe_name),
            )
            return DirectoryResult(success=True, message=f"Created folder {safe_name}")

    def delete_directory(self, rel_path: str) -> DeleteResult:
        """Delete one non-root music directory by relative path."""
        with self._lock:
            current_rel = _normalize_rel_path(rel_path)
            if not current_rel:
                return DeleteResult(
                    success=False, message="Cannot delete root folder", deleted_count=0
                )
            target_dir = self._resolve_directory(current_rel)
            if target_dir == self._music_folder:
                return DeleteResult(
                    success=False, message="Cannot delete root folder", deleted_count=0
                )
            if target_dir.is_symlink() or not target_dir.is_dir():
                return DeleteResult(success=False, message="Folder not found", deleted_count=0)
            try:
                shutil.rmtree(target_dir)
            except OSError as exc:
                raise MusicFileError(f"Failed to delete directory {current_rel}: {exc}") from exc
            logger.info("Deleted music directory %s", current_rel)
            return DeleteResult(success=True, message="Deleted folder", deleted_count=1)

    def move_file(self, source_rel: str, dest_rel: str, new_name: str = "") -> MoveResult:
        """Move one music file into ``dest_rel`` with an optional new name."""
        with self._lock:
            source_path, _ = self._resolve_file_candidate(source_rel)
            if source_path.is_symlink() or not source_path.is_file():
                return MoveResult(
                    success=False,
                    message="Source file not found",
                    destination_path=None,
                )
            final_name = (
                _validate_music_filename(new_name, allowed_extensions=self._allowed_extensions)
                if new_name
                else source_path.name
            )
            destination_dir = self._resolve_directory(dest_rel)
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination_path = destination_dir / final_name
            try:
                os.replace(source_path, destination_path)  # noqa: PTH105 - atomic publish contract
            except OSError as exc:
                raise MusicFileError(
                    f"Failed to move {source_path.name} to {destination_path}: {exc}"
                ) from exc
            destination_rel = _join_relpath(_normalize_rel_path(dest_rel), final_name)
            logger.info("Moved music file %s to %s", source_rel, destination_rel)
            return MoveResult(
                success=True,
                message=f"Moved to {final_name}",
                destination_path=destination_rel,
            )

    def generate_upload_id(self) -> str:
        """Return a 32-character lowercase hex upload id."""
        return secrets.token_hex(16)

    def _resolve_directory(self, rel_path: str) -> Path:
        current_rel = _normalize_rel_path(rel_path)
        if not current_rel:
            return self._music_folder
        return self._music_folder.joinpath(*current_rel.split("/"))

    def _resolve_file_candidate(self, rel_path: str) -> tuple[Path, str]:
        safe_rel = self._normalize_file_relpath(rel_path)
        parts = safe_rel.split("/")
        candidate = self._music_folder.joinpath(*parts)
        return candidate, safe_rel

    def _normalize_file_relpath(self, rel_path: str) -> str:
        raw = rel_path.replace("\\", "/")
        if not raw.strip("/"):
            raise MusicError("Invalid filename")
        folder, _, filename = raw.rpartition("/")
        safe_name = _validate_music_filename(filename, allowed_extensions=self._allowed_extensions)
        safe_folder = _normalize_rel_path(folder)
        return _join_relpath(safe_folder, safe_name)

    def _write_upload_atomically(self, *, source: IO[bytes], destination: Path) -> int:
        measured_size = _measure_stream_size(source)
        self._ensure_free_space(measured_size if measured_size is not None else self._max_file_size)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=str(destination.parent),
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as file_handle:
                size_bytes = _copy_stream_with_limit(
                    source,
                    file_handle,
                    max_size=self._max_file_size,
                )
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(temp_path, destination)  # noqa: PTH105 - atomic publish contract
        except _SizeLimitExceeded:
            _safe_unlink(temp_path)
            raise
        except OSError as exc:
            _safe_unlink(temp_path)
            raise MusicFileError(f"Failed to write {destination}: {exc}") from exc
        return size_bytes

    def _ensure_free_space(self, requested_bytes: int) -> None:
        _, _, free_bytes = self._disk_usage()
        if free_bytes <= requested_bytes + self._free_space_reserve:
            raise MusicError("Not enough free space on Music drive")

    def _disk_usage(self) -> tuple[int, int, int]:
        probe = self._storage_root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
        except OSError as exc:
            raise MusicFileError(f"Failed to read disk usage for {probe}: {exc}") from exc
        return usage.total, usage.used, usage.free


def _secure_filename(name: str | None) -> str:
    if name is None:
        raise MusicError("Invalid filename")
    normalized = name.strip()
    if not normalized:
        raise MusicError("Invalid filename")
    if any(token in normalized for token in _PATH_TRAVERSAL_FORBIDDEN):
        raise MusicError(f"Invalid filename: {name!r}")
    candidate = Path(normalized).name
    if candidate != normalized or candidate in {".", ".."}:
        raise MusicError(f"Invalid filename: {name!r}")
    sanitized = _WHITESPACE_RE.sub(" ", _SANITIZE_RE.sub("", candidate)).strip()
    if not sanitized:
        raise MusicError("Invalid filename")
    return sanitized


def _normalize_rel_path(rel_path: str) -> str:
    raw = rel_path.replace("\\", "/")
    normalized = raw.strip("/")
    if not normalized:
        return ""
    cleaned: list[str] = []
    for segment in normalized.split("/"):
        if segment in {"", ".", ".."}:
            raise MusicError("Invalid folder path")
        cleaned.append(_secure_filename(segment))
    return "/".join(cleaned)


def _validate_music_filename(name: str | None, *, allowed_extensions: tuple[str, ...]) -> str:
    safe_name = _secure_filename(name)
    if Path(safe_name).suffix.lower() not in allowed_extensions:
        raise MusicError("Unsupported file type. Allowed: mp3, flac, wav, aac, m4a")
    return safe_name


def _join_relpath(parent: str, child: str) -> str:
    return f"{parent}/{child}" if parent else child


def _rewind_stream(file_storage: FileStorage) -> IO[bytes]:
    with contextlib.suppress(OSError, ValueError):
        file_storage.stream.seek(0)
    return file_storage.stream


def _append_chunk(source: IO[bytes], *, staged_path: Path, max_chunk_size: int) -> int:
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with staged_path.open("ab") as file_handle:
            chunk = source.read(max_chunk_size + 1)
            if len(chunk) > max_chunk_size:
                raise MusicError(f"Chunk exceeds {max_chunk_size} bytes")
            file_handle.write(chunk)
            file_handle.flush()
            os.fsync(file_handle.fileno())
    except OSError as exc:
        raise MusicFileError(f"Failed to write staged upload {staged_path}: {exc}") from exc
    return len(chunk)


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


def _measure_stream_size(source: IO[bytes]) -> int | None:
    with contextlib.suppress(OSError, ValueError):
        current_position = source.tell()
        source.seek(0, os.SEEK_END)
        size_bytes = source.tell()
        source.seek(current_position)
        return size_bytes
    return None


def _purge_stale_chunks(uploads_dir: Path, *, stale_chunk_age: int) -> None:
    now = datetime.now(tz=UTC).timestamp()
    try:
        for entry in uploads_dir.iterdir():
            if entry.is_symlink() or not entry.is_file() or entry.suffix != ".part":
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age > stale_chunk_age:
                _safe_unlink(entry)
    except OSError:
        logger.debug("Could not purge stale music chunks in %s", uploads_dir, exc_info=True)


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


def _size_limit_message(name: str, size_bytes: int, limit_bytes: int) -> str:
    return (
        f"{name}: File is {size_bytes / 1_048_576:.2f} MiB. "
        f"Limit is {limit_bytes / 1_048_576:.2f} MiB."
    )


def make_music_service(cfg: WebConfig) -> MusicService:
    """Build a music service rooted at the configured Tesla Music folder."""
    return MusicService(
        storage_root=cfg.paths.media_root,
        music_folder=cfg.paths.media_root / cfg.music.folder,
        max_file_size=cfg.music.max_file_size,
        chunk_size=cfg.music.chunk_size,
        free_space_reserve=cfg.music.free_space_reserve,
        stale_chunk_age=cfg.music.stale_chunk_age,
        allowed_extensions=cfg.music.allowed_extensions,
    )


__all__ = (
    "ChunkResult",
    "DeleteResult",
    "DirectoryResult",
    "MoveResult",
    "MusicDirectory",
    "MusicError",
    "MusicFile",
    "MusicFileError",
    "MusicListing",
    "MusicService",
    "UploadResult",
    "make_music_service",
)
