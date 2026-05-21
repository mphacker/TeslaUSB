"""B-1 service: light-show library uploads, listing, deletion, and active selection."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

    from werkzeug.datastructures import FileStorage

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_JSON_ENCODING: Final[str] = "utf-8"
_JSON_INDENT: Final[int] = 2
_COPY_CHUNK_BYTES: Final[int] = 65_536
_LIGHTSHOW_ROOT_DIRNAME: Final[str] = "lightshow"
_PATH_TRAVERSAL_FORBIDDEN: Final[frozenset[str]] = frozenset({"/", "\\", "..", "\x00"})
_ACTIVE_SHOW_FILENAME_KEY: Final[str] = "filename"


class LightShowError(ValueError):
    """The requested light-show operation is invalid."""


class LightShowFileError(OSError):
    """A light-show file or state file could not be read or written."""


class _SizeLimitExceeded(ValueError):
    def __init__(self, size_bytes: int) -> None:
        super().__init__(str(size_bytes))
        self.size_bytes = size_bytes


@dataclass(frozen=True, slots=True)
class UploadResult:
    success: bool
    message: str
    file_count: int


@dataclass(frozen=True, slots=True)
class DeleteResult:
    success: bool
    message: str


@dataclass(frozen=True, slots=True)
class LightShowFile:
    filename: str
    size_bytes: int
    modified_at: datetime


class LightShowService:
    """Manage the B-1 LightShow library and active-selection state."""

    def __init__(
        self,
        *,
        light_show_folder: Path,
        active_show_file: Path,
        max_upload_size: int,
        max_zip_size: int,
        allowed_extensions: tuple[str, ...],
    ) -> None:
        self._light_show_folder = light_show_folder
        self._active_show_file = active_show_file
        self._max_upload_size = max_upload_size
        self._max_zip_size = max_zip_size
        self._allowed_extensions = tuple(extension.lower() for extension in allowed_extensions)
        self._lock = threading.RLock()

    def list_files(self) -> tuple[LightShowFile, ...]:
        """List uploaded light-show files in the library root."""
        with self._lock:
            if not self._light_show_folder.exists():
                return ()
            try:
                files: list[LightShowFile] = []
                for candidate in sorted(
                    self._light_show_folder.iterdir(),
                    key=lambda path: path.name.lower(),
                ):
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    if candidate.suffix.lower() not in self._allowed_extensions:
                        continue
                    stats = candidate.stat()
                    files.append(
                        LightShowFile(
                            filename=candidate.name,
                            size_bytes=stats.st_size,
                            modified_at=datetime.fromtimestamp(stats.st_mtime, tz=UTC),
                        )
                    )
            except OSError as exc:
                raise LightShowFileError(f"Failed to list light show files: {exc}") from exc
            return tuple(files)

    def upload_zip(self, uploaded_file: FileStorage) -> UploadResult:
        """Extract a ZIP upload, recursively flatten supported files, and publish them."""
        with self._lock:
            zip_name = _require_zip_filename(uploaded_file.filename)
            logger.info("Uploading light-show ZIP %s", zip_name)
            try:
                zip_path, zip_size = _write_stream_to_temp_file(
                    source=_rewind_stream(uploaded_file),
                    directory=self._scratch_dir(),
                    max_size=self._max_zip_size,
                    suffix=".zip",
                )
            except _SizeLimitExceeded as exc:
                logger.info(
                    "Rejected oversized light-show ZIP %s (%s bytes)",
                    zip_name,
                    exc.size_bytes,
                )
                return UploadResult(
                    success=False,
                    message=_size_limit_message(
                        label="ZIP file",
                        size_bytes=exc.size_bytes,
                        limit_bytes=self._max_zip_size,
                    ),
                    file_count=0,
                )
            logger.info(
                "Saved light-show ZIP %s (%s bytes) for inspection",
                zip_name,
                zip_size,
            )
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    try:
                        members = self._collect_allowed_members(archive)
                    except _SizeLimitExceeded as exc:
                        return UploadResult(
                            success=False,
                            message=_size_limit_message(
                                label="File",
                                size_bytes=exc.size_bytes,
                                limit_bytes=self._max_upload_size,
                            ),
                            file_count=0,
                        )
                    if not members:
                        return UploadResult(
                            success=False,
                            message="No light show files (.fseq, .mp3, .wav) found in ZIP",
                            file_count=0,
                        )
                    self._light_show_folder.mkdir(parents=True, exist_ok=True)
                    for member, safe_name in members:
                        destination = self._light_show_folder / safe_name
                        with archive.open(member, "r") as source:
                            try:
                                _write_stream_atomically(
                                    source=source,
                                    destination=destination,
                                    max_size=self._max_upload_size,
                                )
                            except _SizeLimitExceeded as exc:
                                logger.info(
                                    "Rejected oversized ZIP member %s (%s bytes)",
                                    safe_name,
                                    exc.size_bytes,
                                )
                                size_message = _size_limit_message(
                                    label="File",
                                    size_bytes=exc.size_bytes,
                                    limit_bytes=self._max_upload_size,
                                )
                                return UploadResult(
                                    success=False,
                                    message=f"{safe_name}: {size_message}",
                                    file_count=0,
                                )
            except zipfile.BadZipFile:
                logger.warning("Invalid ZIP uploaded for light shows: %s", zip_name)
                return UploadResult(success=False, message="Invalid ZIP file", file_count=0)
            finally:
                _safe_unlink(zip_path)
            return UploadResult(
                success=True,
                message=f"Successfully uploaded {len(members)} files from ZIP",
                file_count=len(members),
            )

    def upload_files(self, uploaded_files: Iterable[FileStorage]) -> UploadResult:
        """Save one or more uploaded light-show files into the library root."""
        with self._lock:
            candidates = tuple(uploaded_files)
            if not candidates:
                return UploadResult(success=False, message="No files selected", file_count=0)
            self._light_show_folder.mkdir(parents=True, exist_ok=True)
            successes = 0
            errors: list[str] = []
            for uploaded_file in candidates:
                if not uploaded_file.filename:
                    continue
                try:
                    safe_name = _validate_light_show_filename(
                        uploaded_file.filename,
                        allowed_extensions=self._allowed_extensions,
                    )
                    destination = self._light_show_folder / safe_name
                    try:
                        _write_stream_atomically(
                            source=_rewind_stream(uploaded_file),
                            destination=destination,
                            max_size=self._max_upload_size,
                        )
                    except _SizeLimitExceeded as exc:
                        size_message = _size_limit_message(
                            label="File",
                            size_bytes=exc.size_bytes,
                            limit_bytes=self._max_upload_size,
                        )
                        errors.append(f"{safe_name}: {size_message}")
                        continue
                    successes += 1
                except LightShowError as exc:
                    errors.append(str(exc))
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

    def delete_file(self, filename: str) -> DeleteResult:
        """Delete one library file by filename."""
        with self._lock:
            safe_name = _validate_light_show_filename(
                filename,
                allowed_extensions=self._allowed_extensions,
            )
            return self._delete_file_locked(safe_name)

    def bulk_delete(self, filenames: Iterable[str]) -> DeleteResult:
        """Delete multiple library files and aggregate per-file failures."""
        with self._lock:
            normalized = tuple(
                dict.fromkeys(
                    _validate_light_show_filename(name, allowed_extensions=self._allowed_extensions)
                    for name in filenames
                )
            )
            if not normalized:
                return DeleteResult(success=False, message="No files selected")
            deleted_count = 0
            errors: list[str] = []
            for safe_name in normalized:
                try:
                    result = self._delete_file_locked(safe_name)
                except LightShowFileError as exc:
                    errors.append(f"{safe_name}: {exc}")
                    continue
                if result.success:
                    deleted_count += 1
                else:
                    errors.append(f"{safe_name}: {result.message}")
            if deleted_count == 0:
                if errors:
                    return DeleteResult(success=False, message="; ".join(errors))
                return DeleteResult(success=False, message="No files selected")
            if errors:
                return DeleteResult(
                    success=True,
                    message=f"Deleted {deleted_count} file(s). Errors: {'; '.join(errors)}",
                )
            return DeleteResult(success=True, message=f"Deleted {deleted_count} file(s)")

    def get_active_show(self) -> str | None:
        """Return the currently selected active light-show file, if it still exists."""
        with self._lock:
            active_name = self._load_active_show_locked()
            if active_name is None:
                return None
            candidate = self._light_show_folder / active_name
            if candidate.is_symlink() or not candidate.is_file():
                logger.info("Active light-show selection %s is stale", active_name)
                return None
            if candidate.suffix.lower() not in self._allowed_extensions:
                logger.warning(
                    "Active light-show selection %s has an unsupported extension",
                    active_name,
                )
                return None
            return active_name

    def set_active_show(self, filename: str) -> None:
        """Persist the active light-show filename after validating it exists."""
        with self._lock:
            safe_name = _validate_light_show_filename(
                filename,
                allowed_extensions=self._allowed_extensions,
            )
            candidate = self._light_show_folder / safe_name
            if candidate.is_symlink() or not candidate.is_file():
                raise LightShowError(f"Light show file not found: {safe_name}")
            _write_json_atomically(
                path=self._active_show_file,
                payload={_ACTIVE_SHOW_FILENAME_KEY: safe_name},
            )
            logger.info("Set active light show to %s", safe_name)

    def _collect_allowed_members(
        self,
        archive: zipfile.ZipFile,
    ) -> list[tuple[zipfile.ZipInfo, str]]:
        members: list[tuple[zipfile.ZipInfo, str]] = []
        for member in archive.infolist():
            if member.is_dir():
                continue
            flattened_name = _flatten_zip_member_name(member.filename)
            if not flattened_name:
                continue
            if not _has_allowed_extension(flattened_name, self._allowed_extensions):
                continue
            safe_name = _validate_light_show_filename(
                flattened_name,
                allowed_extensions=self._allowed_extensions,
            )
            if member.file_size > self._max_upload_size:
                raise _SizeLimitExceeded(member.file_size)
            members.append((member, safe_name))
        logger.info("Found %s supported light-show files in uploaded ZIP", len(members))
        return members

    def _delete_file_locked(self, safe_name: str) -> DeleteResult:
        candidate = self._light_show_folder / safe_name
        if candidate.is_symlink() or not candidate.is_file():
            return DeleteResult(success=False, message=f"File not found: {safe_name}")
        try:
            candidate.unlink()
        except OSError as exc:
            message = f"Failed to delete light show file {safe_name}: {exc}"
            raise LightShowFileError(message) from exc
        if self._load_active_show_locked() == safe_name:
            _write_json_atomically(
                path=self._active_show_file,
                payload={_ACTIVE_SHOW_FILENAME_KEY: None},
            )
        logger.info("Deleted light-show file %s", safe_name)
        return DeleteResult(success=True, message=f"Successfully deleted {safe_name}")

    def _load_active_show_locked(self) -> str | None:
        if not self._active_show_file.exists():
            return None
        try:
            raw_text = self._active_show_file.read_text(encoding=_JSON_ENCODING)
        except OSError as exc:
            raise LightShowFileError(f"Failed to read {self._active_show_file}: {exc}") from exc
        try:
            payload: object = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise LightShowFileError(f"Failed to parse {self._active_show_file}: {exc}") from exc
        if not isinstance(payload, dict):
            raise LightShowFileError("Active show state file must contain a JSON object")
        active_name = payload.get(_ACTIVE_SHOW_FILENAME_KEY)
        if active_name is None:
            return None
        if not isinstance(active_name, str):
            raise LightShowFileError("Active show filename must be a string or null")
        try:
            return _validate_light_show_filename(
                active_name,
                allowed_extensions=self._allowed_extensions,
            )
        except LightShowError as exc:
            raise LightShowFileError(f"Active show filename is invalid: {exc}") from exc

    def _scratch_dir(self) -> Path:
        return self._active_show_file.parent / ".light_show_tmp"


def _require_zip_filename(filename: str | None) -> str:
    safe_name = _validate_plain_filename(filename)
    if Path(safe_name).suffix.lower() != ".zip":
        raise LightShowError("Filename must end with .zip")
    return safe_name


def _validate_light_show_filename(name: str | None, *, allowed_extensions: tuple[str, ...]) -> str:
    safe_name = _validate_plain_filename(name)
    if not _has_allowed_extension(safe_name, allowed_extensions):
        raise LightShowError("Only fseq, mp3, and wav files are allowed")
    return safe_name


def _validate_plain_filename(name: str | None) -> str:
    if name is None:
        raise LightShowError("Filename is required")
    normalized = name.strip()
    if not normalized:
        raise LightShowError("Filename is required")
    if any(token in normalized for token in _PATH_TRAVERSAL_FORBIDDEN):
        raise LightShowError(f"Invalid filename: {name!r}")
    candidate = Path(normalized).name
    if candidate != normalized or candidate in {".", ".."}:
        raise LightShowError(f"Invalid filename: {name!r}")
    return candidate


def _has_allowed_extension(name: str, allowed_extensions: tuple[str, ...]) -> bool:
    return Path(name).suffix.lower() in allowed_extensions


def _flatten_zip_member_name(member_name: str) -> str:
    return PurePosixPath(member_name.replace("\\", "/")).name


def _rewind_stream(file_storage: FileStorage) -> IO[bytes]:
    with contextlib.suppress(OSError, ValueError):
        file_storage.stream.seek(0)
    return file_storage.stream


def _write_stream_to_temp_file(
    *,
    source: IO[bytes],
    directory: Path,
    max_size: int,
    suffix: str,
) -> tuple[Path, int]:
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(directory), prefix="lightshow-", suffix=suffix)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as file_handle:
            size_bytes = _copy_stream_with_limit(source, file_handle, max_size=max_size)
            file_handle.flush()
            os.fsync(file_handle.fileno())
    except _SizeLimitExceeded:
        _safe_unlink(temp_path)
        raise
    except OSError as exc:
        _safe_unlink(temp_path)
        raise LightShowFileError(f"Failed to write temporary file {temp_path}: {exc}") from exc
    return temp_path, size_bytes


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
            os.fsync(file_handle.fileno())
        os.replace(temp_path, destination)  # noqa: PTH105 - atomic publish contract
    except _SizeLimitExceeded:
        _safe_unlink(temp_path)
        raise
    except OSError as exc:
        _safe_unlink(temp_path)
        raise LightShowFileError(f"Failed to write {destination}: {exc}") from exc
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


def _write_json_atomically(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    raw_json = json.dumps(payload, indent=_JSON_INDENT, sort_keys=True) + "\n"
    try:
        with os.fdopen(fd, "w", encoding=_JSON_ENCODING, newline="\n") as file_handle:
            file_handle.write(raw_json)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temp_path, path)  # noqa: PTH105 - atomic publish contract
    except OSError as exc:
        _safe_unlink(temp_path)
        raise LightShowFileError(f"Failed to write {path}: {exc}") from exc


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


def _size_limit_message(*, label: str, size_bytes: int, limit_bytes: int) -> str:
    return f"{label} is {size_bytes / 1_048_576:.2f} MB. Limit is {limit_bytes / 1_048_576:.2f} MB."


def make_light_show_service(cfg: WebConfig) -> LightShowService:
    """Build a light-show service using the configured backing-root and state paths."""
    lightshow_root = cfg.paths.backing_root / _LIGHTSHOW_ROOT_DIRNAME
    return LightShowService(
        light_show_folder=lightshow_root / cfg.light_shows.folder,
        active_show_file=cfg.paths.state_dir / cfg.light_shows.active_show_relpath,
        max_upload_size=cfg.light_shows.max_upload_size,
        max_zip_size=cfg.light_shows.max_zip_size,
        allowed_extensions=cfg.light_shows.allowed_extensions,
    )


__all__ = (
    "DeleteResult",
    "LightShowError",
    "LightShowFile",
    "LightShowFileError",
    "LightShowService",
    "UploadResult",
    "make_light_show_service",
)
