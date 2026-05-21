"""B-1 service: Tesla custom-wrap listing, validation, upload, and deletion."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import struct
import tempfile
import threading
import zlib
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
_BYTES_PER_MIB: Final[int] = 1_048_576
_LIGHTSHOW_ROOT_DIRNAME: Final[str] = "lightshow"
_PATH_TRAVERSAL_FORBIDDEN: Final[frozenset[str]] = frozenset({"/", "\\", "..", "\x00"})
_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_PNG_UINT32_BYTES: Final[int] = 4
_IHDR_CHUNK_TYPE: Final[bytes] = b"IHDR"
_IHDR_CHUNK_LENGTH: Final[int] = 13
_VALID_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\- ]+$")


class WrapError(ValueError):
    """The requested wrap operation is invalid."""


class WrapFileError(OSError):
    """A wrap file or directory could not be read or written."""


class _SizeLimitExceeded(ValueError):
    def __init__(self, size_bytes: int) -> None:
        super().__init__(str(size_bytes))
        self.size_bytes = size_bytes


@dataclass(frozen=True, slots=True)
class WrapInfo:
    filename: str
    size_bytes: int
    width: int | None
    height: int | None
    modified_at: datetime


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


@dataclass(frozen=True, slots=True)
class ValidationResult:
    success: bool
    message: str
    width: int | None
    height: int | None
    size_bytes: int


class WrapService:
    """Manage Tesla custom wrap PNGs stored at the lightshow USB root."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        wraps_folder: Path,
        max_size: int,
        min_dimension: int,
        max_dimension: int,
        max_filename_length: int,
        max_upload_count: int,
        allowed_extensions: tuple[str, ...],
    ) -> None:
        self._wraps_folder = wraps_folder
        self._max_size = max_size
        self._min_dimension = min_dimension
        self._max_dimension = max_dimension
        self._max_filename_length = max_filename_length
        self._max_upload_count = max_upload_count
        self._allowed_extensions = tuple(extension.lower() for extension in allowed_extensions)
        self._lock = threading.RLock()

    def list_wraps(self) -> tuple[WrapInfo, ...]:
        """List uploaded wraps, sorted case-insensitively by filename."""
        with self._lock:
            if not self._wraps_folder.exists():
                return ()
            try:
                wraps: list[WrapInfo] = []
                for candidate in sorted(
                    self._wraps_folder.iterdir(),
                    key=lambda path: path.name.lower(),
                ):
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    if candidate.suffix.lower() not in self._allowed_extensions:
                        continue
                    try:
                        stats = candidate.stat()
                        width, height = _read_png_dimensions(candidate)
                    except OSError as exc:
                        logger.warning("Could not read wrap file %s: %s", candidate.name, exc)
                        continue
                    wraps.append(
                        WrapInfo(
                            filename=candidate.name,
                            size_bytes=stats.st_size,
                            width=width,
                            height=height,
                            modified_at=datetime.fromtimestamp(stats.st_mtime, tz=UTC),
                        )
                    )
            except OSError as exc:
                raise WrapFileError(f"Failed to list wrap files: {exc}") from exc
            return tuple(wraps)

    def get_wrap_count(self) -> int:
        """Return the number of uploaded wrap PNGs currently stored."""
        with self._lock:
            return len(self.list_wraps())

    def validate_png(self, file_path: Path) -> ValidationResult:
        """Validate a PNG file on disk against Tesla wrap constraints."""
        with self._lock:
            try:
                size_bytes = file_path.stat().st_size
            except OSError as exc:
                raise WrapFileError(f"Failed to stat wrap file {file_path}: {exc}") from exc
            if size_bytes > self._max_size:
                return ValidationResult(
                    success=False,
                    message=(
                        f"File size must be 1 MB or less (got {size_bytes / _BYTES_PER_MIB:.2f} MB)"
                    ),
                    width=None,
                    height=None,
                    size_bytes=size_bytes,
                )
            width, height = _read_png_dimensions(file_path)
            if width is None or height is None:
                return ValidationResult(
                    success=False,
                    message="Could not read image dimensions - file may be corrupted",
                    width=None,
                    height=None,
                    size_bytes=size_bytes,
                )
            if width != height:
                return ValidationResult(
                    success=False,
                    message=f"Image dimensions must be square (got {width}x{height})",
                    width=width,
                    height=height,
                    size_bytes=size_bytes,
                )
            if width < self._min_dimension or height < self._min_dimension:
                return ValidationResult(
                    success=False,
                    message=(
                        "Image dimensions must be at least "
                        f"{self._min_dimension}x{self._min_dimension} (got {width}x{height})"
                    ),
                    width=width,
                    height=height,
                    size_bytes=size_bytes,
                )
            if width > self._max_dimension or height > self._max_dimension:
                return ValidationResult(
                    success=False,
                    message=(
                        "Image dimensions must not exceed "
                        f"{self._max_dimension}x{self._max_dimension} (got {width}x{height})"
                    ),
                    width=width,
                    height=height,
                    size_bytes=size_bytes,
                )
            return ValidationResult(
                success=True,
                message="Valid PNG",
                width=width,
                height=height,
                size_bytes=size_bytes,
            )

    def upload_files(self, uploaded_files: Iterable[FileStorage]) -> UploadResult:
        """Validate and atomically publish one or more uploaded wrap PNGs."""
        with self._lock:
            candidates = tuple(file for file in uploaded_files if file.filename)
            if not candidates:
                return UploadResult(success=False, message="No files selected", file_count=0)
            if len(candidates) > self._max_upload_count:
                return UploadResult(
                    success=False,
                    message=f"You can upload at most {self._max_upload_count} wraps at once",
                    file_count=0,
                )
            self._wraps_folder.mkdir(parents=True, exist_ok=True)
            successes = 0
            errors: list[str] = []
            for uploaded_file in candidates:
                raw_name = uploaded_file.filename
                if raw_name is None:
                    continue
                try:
                    safe_name = _validate_wrap_filename(
                        raw_name,
                        allowed_extensions=self._allowed_extensions,
                        max_filename_length=self._max_filename_length,
                    )
                    size_bytes = self._publish_upload(uploaded_file, self._wraps_folder / safe_name)
                except _SizeLimitExceeded as exc:
                    errors.append(
                        f"{raw_name}: File size must be 1 MB or less "
                        f"(got {exc.size_bytes / _BYTES_PER_MIB:.2f} MB)"
                    )
                    continue
                except WrapError as exc:
                    errors.append(str(exc))
                    continue
                logger.info("Uploaded wrap %s (%s bytes)", safe_name, size_bytes)
                successes += 1
            if successes == 0:
                if errors:
                    return UploadResult(success=False, message="; ".join(errors), file_count=0)
                return UploadResult(success=False, message="No files selected", file_count=0)
            if errors:
                return UploadResult(
                    success=True,
                    message=(
                        f"Successfully uploaded {successes} wrap(s). Errors: {'; '.join(errors)}"
                    ),
                    file_count=successes,
                )
            return UploadResult(
                success=True,
                message=f"Successfully uploaded {successes} wrap(s)",
                file_count=successes,
            )

    def delete_wrap(self, filename: str) -> DeleteResult:
        """Delete one wrap PNG by filename."""
        with self._lock:
            safe_name = _validate_wrap_filename(
                filename,
                allowed_extensions=self._allowed_extensions,
                max_filename_length=self._max_filename_length,
            )
            return self._delete_wrap_locked(safe_name)

    def bulk_delete(self, filenames: Iterable[str]) -> DeleteResult:
        """Delete multiple wrap PNGs and aggregate missing-file errors."""
        with self._lock:
            normalized = tuple(
                dict.fromkeys(
                    _validate_wrap_filename(
                        filename,
                        allowed_extensions=self._allowed_extensions,
                        max_filename_length=self._max_filename_length,
                    )
                    for filename in filenames
                )
            )
            if not normalized:
                return DeleteResult(success=False, message="No files selected", deleted_count=0)
            deleted_count = 0
            errors: list[str] = []
            for safe_name in normalized:
                try:
                    result = self._delete_wrap_locked(safe_name)
                except WrapFileError as exc:
                    errors.append(f"{safe_name}: {exc}")
                    continue
                if result.success:
                    deleted_count += 1
                else:
                    errors.append(f"{safe_name}: {result.message}")
            if deleted_count == 0:
                if errors:
                    return DeleteResult(success=False, message="; ".join(errors), deleted_count=0)
                return DeleteResult(success=False, message="No files selected", deleted_count=0)
            if errors:
                return DeleteResult(
                    success=True,
                    message=f"Deleted {deleted_count} wrap(s). Errors: {'; '.join(errors)}",
                    deleted_count=deleted_count,
                )
            return DeleteResult(
                success=True,
                message=f"Deleted {deleted_count} wrap(s)",
                deleted_count=deleted_count,
            )

    def _publish_upload(self, uploaded_file: FileStorage, destination: Path) -> int:
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
                    _rewind_stream(uploaded_file),
                    file_handle,
                    max_size=self._max_size,
                )
                file_handle.flush()
                os.fsync(file_handle.fileno())
            validation = self.validate_png(temp_path)
            if not validation.success:
                raise WrapError(f"{destination.name}: {validation.message}")
            os.replace(temp_path, destination)  # noqa: PTH105 - atomic publish contract
        except _SizeLimitExceeded:
            _safe_unlink(temp_path)
            raise
        except OSError as exc:
            _safe_unlink(temp_path)
            raise WrapFileError(f"Failed to write {destination}: {exc}") from exc
        except WrapError:
            _safe_unlink(temp_path)
            raise
        return size_bytes

    def _delete_wrap_locked(self, safe_name: str) -> DeleteResult:
        candidate = self._wraps_folder / safe_name
        if candidate.is_symlink() or not candidate.is_file():
            return DeleteResult(success=False, message="File not found", deleted_count=0)
        try:
            candidate.unlink()
        except OSError as exc:
            raise WrapFileError(f"Failed to delete wrap {safe_name}: {exc}") from exc
        logger.info("Deleted wrap %s", safe_name)
        return DeleteResult(success=True, message=f"Deleted {safe_name}", deleted_count=1)


def _secure_filename(name: str | None) -> str:
    if name is None:
        raise WrapError("Filename is required")
    normalized = name.strip()
    if not normalized:
        raise WrapError("Filename is required")
    if any(token in normalized for token in _PATH_TRAVERSAL_FORBIDDEN):
        raise WrapError(f"Invalid filename: {name!r}")
    candidate = Path(normalized).name
    if candidate != normalized or candidate in {".", ".."}:
        raise WrapError(f"Invalid filename: {name!r}")
    return candidate


def _validate_wrap_filename(
    name: str | None,
    *,
    allowed_extensions: tuple[str, ...],
    max_filename_length: int,
) -> str:
    safe_name = _secure_filename(name)
    if Path(safe_name).suffix.lower() not in allowed_extensions:
        raise WrapError("Only PNG files are allowed")
    stem = Path(safe_name).stem
    if len(stem) > max_filename_length:
        raise WrapError(
            f"Filename must be {max_filename_length} characters or less (currently {len(stem)})"
        )
    if not stem:
        raise WrapError("Filename cannot be empty")
    if _VALID_FILENAME_PATTERN.fullmatch(stem) is None:
        raise WrapError(
            "Filename can only contain letters, numbers, underscores, dashes, and spaces"
        )
    return safe_name


def _read_png_dimensions(file_path: Path) -> tuple[int | None, int | None]:
    try:
        with file_path.open("rb") as file_handle:
            if file_handle.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
                return None, None
            chunk_length_raw = file_handle.read(_PNG_UINT32_BYTES)
            chunk_type = file_handle.read(_PNG_UINT32_BYTES)
            if len(chunk_length_raw) != _PNG_UINT32_BYTES or len(chunk_type) != _PNG_UINT32_BYTES:
                return None, None
            chunk_length = struct.unpack(">I", chunk_length_raw)[0]
            if chunk_length != _IHDR_CHUNK_LENGTH or chunk_type != _IHDR_CHUNK_TYPE:
                return None, None
            chunk_data = file_handle.read(chunk_length)
            chunk_crc_raw = file_handle.read(_PNG_UINT32_BYTES)
            if len(chunk_data) != chunk_length or len(chunk_crc_raw) != _PNG_UINT32_BYTES:
                return None, None
            expected_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
            actual_crc = struct.unpack(">I", chunk_crc_raw)[0]
            if actual_crc != expected_crc:
                return None, None
            return struct.unpack(">II", chunk_data[:8])
    except OSError:
        raise


def _rewind_stream(file_storage: FileStorage) -> IO[bytes]:
    with contextlib.suppress(OSError, ValueError):
        file_storage.stream.seek(0)
    return file_storage.stream


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


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


def make_wrap_service(cfg: WebConfig) -> WrapService:
    """Build a wrap service rooted at the lightshow USB backing directory."""
    lightshow_root = cfg.paths.backing_root / _LIGHTSHOW_ROOT_DIRNAME
    return WrapService(
        wraps_folder=lightshow_root / cfg.wraps.folder,
        max_size=cfg.wraps.max_size,
        min_dimension=cfg.wraps.min_dimension,
        max_dimension=cfg.wraps.max_dimension,
        max_filename_length=cfg.wraps.max_filename_length,
        max_upload_count=cfg.wraps.max_upload_count,
        allowed_extensions=cfg.wraps.allowed_extensions,
    )


__all__ = (
    "DeleteResult",
    "UploadResult",
    "ValidationResult",
    "WrapError",
    "WrapFileError",
    "WrapInfo",
    "WrapService",
    "make_wrap_service",
)
