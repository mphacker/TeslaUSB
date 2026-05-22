"""B-1 service: Tesla custom license-plate PNG storage.

This service manages the PNG background images that Tesla's in-car
Background -> Image selector picks up from the ``LicensePlate/``
folder on the lightshow USB partition. It is the photo half of the
license-plate feature; the tracked-plate text database lives in
:mod:`teslausb_web.services.license_plate_service`.

Tesla's published spec (custom-wraps issue #13):

* PNG format only
* 420x75 (North America) or 492x75 (Europe / Italy)
* 512 KB maximum file size
* Base filename: up to 12 alphanumeric characters
* Up to 5 plates stored at a time

The validation enforces this spec strictly on the server side. If
the spec ever loosens we can relax in one place. The folder lives at
``{backing_root}/lightshow/LicensePlate`` and is created on demand.

Patterns mirror :mod:`teslausb_web.services.wrap_service` (PNG
signature + IHDR parsing without Pillow, atomic publish via tempfile
+ ``os.replace``, threading lock around all mutating ops).
"""

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
_BYTES_PER_KIB: Final[int] = 1_024
_LIGHTSHOW_ROOT_DIRNAME: Final[str] = "lightshow"
_PLATES_DIRNAME: Final[str] = "LicensePlate"
_PARTITION_KEY: Final[str] = "LightShow"
_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_PNG_UINT32_BYTES: Final[int] = 4
_IHDR_CHUNK_TYPE: Final[bytes] = b"IHDR"
_IHDR_CHUNK_LENGTH: Final[int] = 13
_PATH_TRAVERSAL_FORBIDDEN: Final[frozenset[str]] = frozenset({"/", "\\", "..", "\x00"})
_VALID_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9]+$")

# Tesla published dimensions. (width, height) pairs the firmware accepts.
_PLATE_DIMENSIONS_NA: Final[tuple[int, int]] = (420, 75)
_PLATE_DIMENSIONS_EU: Final[tuple[int, int]] = (492, 75)
_PLATE_DIMENSIONS_ALLOWED: Final[tuple[tuple[int, int], ...]] = (
    _PLATE_DIMENSIONS_NA,
    _PLATE_DIMENSIONS_EU,
)

_DEFAULT_MAX_FILE_SIZE: Final[int] = 512 * _BYTES_PER_KIB
_DEFAULT_MAX_FILENAME_LENGTH: Final[int] = 12
_DEFAULT_MAX_PLATE_COUNT: Final[int] = 5
_ALLOWED_EXTENSIONS: Final[tuple[str, ...]] = (".png",)


class PhotoPlateError(ValueError):
    """The requested photo-plate operation is invalid."""


class PhotoPlateFileError(OSError):
    """A photo-plate file or directory could not be read or written."""


class _SizeLimitExceeded(ValueError):
    def __init__(self, size_bytes: int) -> None:
        super().__init__(str(size_bytes))
        self.size_bytes = size_bytes


@dataclass(frozen=True, slots=True)
class PhotoPlateFile:
    """One PNG stored under the LicensePlate folder."""

    filename: str
    size_bytes: int
    width: int | None
    height: int | None
    modified_at: datetime
    partition_key: str = _PARTITION_KEY

    @property
    def dimensions(self) -> str:
        if self.width is None or self.height is None:
            return "unknown"
        return f"{self.width}x{self.height}"

    @property
    def size_str(self) -> str:
        if self.size_bytes < _BYTES_PER_KIB:
            return f"{self.size_bytes} B"
        return f"{self.size_bytes / _BYTES_PER_KIB:.1f} KB"

    @property
    def issues(self) -> tuple[str, ...]:
        problems: list[str] = []
        if self.width is None or self.height is None:
            problems.append("PNG header could not be read")
        elif (self.width, self.height) not in _PLATE_DIMENSIONS_ALLOWED:
            problems.append(
                f"Dimensions {self.width}x{self.height} do not match Tesla's "
                f"{_PLATE_DIMENSIONS_NA[0]}x{_PLATE_DIMENSIONS_NA[1]} or "
                f"{_PLATE_DIMENSIONS_EU[0]}x{_PLATE_DIMENSIONS_EU[1]} spec"
            )
        if self.size_bytes > _DEFAULT_MAX_FILE_SIZE:
            problems.append(
                f"Size {self.size_bytes / _BYTES_PER_KIB:.1f} KB exceeds Tesla's "
                f"{_DEFAULT_MAX_FILE_SIZE // _BYTES_PER_KIB} KB limit"
            )
        return tuple(problems)

    @property
    def compliant(self) -> bool:
        return not self.issues


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


class PhotoPlateService:
    """Manage the Tesla custom-license-plate PNGs on the lightshow root."""

    def __init__(
        self,
        *,
        plates_folder: Path,
        max_file_size: int = _DEFAULT_MAX_FILE_SIZE,
        max_filename_length: int = _DEFAULT_MAX_FILENAME_LENGTH,
        max_plate_count: int = _DEFAULT_MAX_PLATE_COUNT,
    ) -> None:
        self._plates_folder = plates_folder
        self._max_file_size = max_file_size
        self._max_filename_length = max_filename_length
        self._max_plate_count = max_plate_count
        self._lock = threading.Lock()

    @property
    def plates_folder(self) -> Path:
        return self._plates_folder

    @property
    def partition_key(self) -> str:
        return _PARTITION_KEY

    def list_plates(self) -> tuple[PhotoPlateFile, ...]:
        with self._lock:
            if not self._plates_folder.exists():
                return ()
            try:
                entries = sorted(
                    self._plates_folder.iterdir(),
                    key=lambda candidate: candidate.name.lower(),
                )
            except OSError as exc:
                raise PhotoPlateFileError(f"Failed to list plates: {exc}") from exc
            plates: list[PhotoPlateFile] = []
            for candidate in entries:
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                if candidate.suffix.lower() not in _ALLOWED_EXTENSIONS:
                    continue
                try:
                    stats = candidate.stat()
                    width, height = _read_png_dimensions(candidate)
                except OSError as exc:
                    logger.warning("Could not read plate %s: %s", candidate.name, exc)
                    continue
                plates.append(
                    PhotoPlateFile(
                        filename=candidate.name,
                        size_bytes=stats.st_size,
                        width=width,
                        height=height,
                        modified_at=datetime.fromtimestamp(stats.st_mtime, tz=UTC),
                    )
                )
            return tuple(plates)

    def count_plates(self) -> int:
        return len(self.list_plates())

    def resolve_plate(self, filename: str) -> Path:
        """Return the on-disk path for an existing plate, or raise."""
        safe_name = _validate_plate_filename(
            filename, max_filename_length=self._max_filename_length
        )
        candidate = self._plates_folder / safe_name
        if candidate.is_symlink() or not candidate.is_file():
            raise PhotoPlateError(f"Plate {safe_name!r} not found")
        return candidate

    def upload_files(self, uploaded_files: Iterable[FileStorage]) -> UploadResult:
        """Validate and atomically publish one or more plate PNGs."""
        with self._lock:
            candidates = tuple(file for file in uploaded_files if file.filename)
            if not candidates:
                return UploadResult(success=False, message="No files selected", file_count=0)
            existing_count = self._count_locked()
            available_slots = max(self._max_plate_count - existing_count, 0)
            if available_slots <= 0:
                return UploadResult(
                    success=False,
                    message=(
                        f"Maximum of {self._max_plate_count} plates allowed "
                        f"(currently have {existing_count}). Delete one first."
                    ),
                    file_count=0,
                )
            self._plates_folder.mkdir(parents=True, exist_ok=True)
            successes = 0
            errors: list[str] = []
            for uploaded_file in candidates:
                raw_name = uploaded_file.filename
                if raw_name is None:
                    continue
                if successes >= available_slots:
                    errors.append(
                        f"{raw_name}: would exceed Tesla's {self._max_plate_count}-plate limit"
                    )
                    continue
                try:
                    safe_name = _validate_plate_filename(
                        raw_name, max_filename_length=self._max_filename_length
                    )
                    size_bytes = self._publish_upload(
                        uploaded_file, self._plates_folder / safe_name
                    )
                except _SizeLimitExceeded as exc:
                    errors.append(
                        f"{raw_name}: file size must be "
                        f"{self._max_file_size // _BYTES_PER_KIB} KB or less "
                        f"(got {exc.size_bytes / _BYTES_PER_KIB:.1f} KB)"
                    )
                    continue
                except PhotoPlateError as exc:
                    errors.append(str(exc))
                    continue
                logger.info("Uploaded plate %s (%s bytes)", safe_name, size_bytes)
                successes += 1
            if successes == 0:
                if errors:
                    return UploadResult(success=False, message="; ".join(errors), file_count=0)
                return UploadResult(success=False, message="No files selected", file_count=0)
            if errors:
                return UploadResult(
                    success=True,
                    message=(f"Uploaded {successes} plate(s). Errors: {'; '.join(errors)}"),
                    file_count=successes,
                )
            return UploadResult(
                success=True,
                message=f"Uploaded {successes} plate(s)",
                file_count=successes,
            )

    def delete_plate(self, filename: str) -> DeleteResult:
        with self._lock:
            safe_name = _validate_plate_filename(
                filename, max_filename_length=self._max_filename_length
            )
            candidate = self._plates_folder / safe_name
            if candidate.is_symlink() or not candidate.is_file():
                return DeleteResult(success=False, message="File not found", deleted_count=0)
            try:
                candidate.unlink()
            except OSError as exc:
                raise PhotoPlateFileError(f"Failed to delete plate {safe_name}: {exc}") from exc
            logger.info("Deleted plate %s", safe_name)
            return DeleteResult(success=True, message=f"Deleted {safe_name}", deleted_count=1)

    def _count_locked(self) -> int:
        if not self._plates_folder.exists():
            return 0
        try:
            return sum(
                1
                for candidate in self._plates_folder.iterdir()
                if candidate.is_file()
                and not candidate.is_symlink()
                and candidate.suffix.lower() in _ALLOWED_EXTENSIONS
            )
        except OSError:
            return 0

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
                    max_size=self._max_file_size,
                )
                file_handle.flush()
                os.fsync(file_handle.fileno())
            width, height = _read_png_dimensions(temp_path)
            if width is None or height is None:
                raise PhotoPlateError(f"{destination.name}: not a valid PNG file")
            if (width, height) not in _PLATE_DIMENSIONS_ALLOWED:
                raise PhotoPlateError(
                    f"{destination.name}: dimensions {width}x{height} do not match Tesla's "
                    f"{_PLATE_DIMENSIONS_NA[0]}x{_PLATE_DIMENSIONS_NA[1]} (NA) or "
                    f"{_PLATE_DIMENSIONS_EU[0]}x{_PLATE_DIMENSIONS_EU[1]} (EU) spec"
                )
            os.replace(temp_path, destination)  # noqa: PTH105 - atomic publish contract
        except _SizeLimitExceeded:
            _safe_unlink(temp_path)
            raise
        except OSError as exc:
            _safe_unlink(temp_path)
            raise PhotoPlateFileError(f"Failed to write {destination}: {exc}") from exc
        except PhotoPlateError:
            _safe_unlink(temp_path)
            raise
        return size_bytes


def _secure_filename(name: str | None) -> str:
    if name is None:
        raise PhotoPlateError("Filename is required")
    normalized = name.strip()
    if not normalized:
        raise PhotoPlateError("Filename is required")
    if any(token in normalized for token in _PATH_TRAVERSAL_FORBIDDEN):
        raise PhotoPlateError(f"Invalid filename: {name!r}")
    candidate = Path(normalized).name
    if candidate != normalized or candidate in {".", ".."}:
        raise PhotoPlateError(f"Invalid filename: {name!r}")
    return candidate


def _validate_plate_filename(name: str | None, *, max_filename_length: int) -> str:
    safe_name = _secure_filename(name)
    if Path(safe_name).suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise PhotoPlateError("Only PNG files are allowed")
    stem = Path(safe_name).stem
    if not stem:
        raise PhotoPlateError("Filename cannot be empty")
    if len(stem) > max_filename_length:
        raise PhotoPlateError(
            f"Filename {stem!r} is {len(stem)} characters; Tesla allows up to "
            f"{max_filename_length} alphanumeric characters (no spaces, dashes, or underscores)"
        )
    if _VALID_FILENAME_PATTERN.fullmatch(stem) is None:
        raise PhotoPlateError(
            f"Filename {stem!r} must contain only letters and digits "
            f"(no spaces, dashes, or underscores)"
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


def make_photo_plate_service(cfg: WebConfig) -> PhotoPlateService:
    """Build a photo-plate service rooted at the lightshow USB backing directory."""
    return PhotoPlateService(
        plates_folder=cfg.paths.backing_root / _LIGHTSHOW_ROOT_DIRNAME / _PLATES_DIRNAME,
    )


__all__ = (
    "DeleteResult",
    "PhotoPlateError",
    "PhotoPlateFile",
    "PhotoPlateFileError",
    "PhotoPlateService",
    "UploadResult",
    "make_photo_plate_service",
)
