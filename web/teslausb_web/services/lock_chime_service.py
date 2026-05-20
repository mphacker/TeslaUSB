"""B-1 service: lock chime file lifecycle (validate, re-encode, normalize, list, delete).

Pure I/O + audio-processing layer. Does NOT import flask; imports only
``werkzeug.utils.secure_filename`` for filename sanitisation because the web
project already depends on Werkzeug via Flask and the helper is pure Python.
Callers (blueprints) own cache-invalidation scheduling.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess  # nosec B404
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, cast

from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

_FFMPEG_TIMEOUT_SEC: Final[int] = 30
_MD5_CHUNK_BYTES: Final[int] = 65_536
_TARGET_LUFS_DEFAULT: Final[int] = -16
_WAV_TARGET_SAMPLE_RATE: Final[int] = 44_100
_WAV_TARGET_BITS: Final[int] = 16
_WAV_REQUIRED_SAMPLE_RATES: Final[frozenset[int]] = frozenset({44_100, 48_000})
_WAV_REQUIRED_CHANNELS: Final[frozenset[int]] = frozenset({1, 2})
_TRIM_HEADER_OVERHEAD_BYTES: Final[int] = 200
_PATH_TRAVERSAL_FORBIDDEN: Final[frozenset[str]] = frozenset({"/", "\\", "..", "\x00"})
_BYTES_PER_MIB: Final[int] = 1_048_576
_MAX_LOCK_CHIME_SIZE_BYTES: Final[int] = 1_048_576
_PCM_BYTES_PER_SAMPLE: Final[int] = 2
_PCM_MONO_CHANNELS: Final[int] = 1
_WAV_HEADER_MIN_BYTES: Final[int] = 12
_RIFF_MAGIC: Final[bytes] = b"RIFF"
_WAVE_MAGIC: Final[bytes] = b"WAVE"
_WAV_SUFFIX: Final[str] = ".wav"
_LOCK_CHIME_FILENAME_DEFAULT: Final[str] = "LockChime.wav"
_OUTPUT_FILE_MISSING_BYTES: Final[int] = 0
_FFMPEG_ERROR_LINES: Final[int] = 3
_FFMPEG_ERROR_MAX_CHARS: Final[int] = 300
_PROGRESS_ATTEMPT_PREFIX: Final[str] = "Attempt"

_ProgressCallback = Callable[[str], None]


class LockChimeAudioError(ValueError):
    """Audio-processing failure while invoking FFmpeg or parsing its output."""


class LockChimeFileError(OSError):
    """Filesystem failure while reading, writing, or replacing a chime file."""


@dataclass(frozen=True, slots=True)
class WavValidation:
    """Result of Tesla WAV validation."""

    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class ReencodeResult:
    """Result of an FFmpeg Tesla re-encode attempt."""

    ok: bool
    message: str
    strategy: str | None
    attempt: int | None
    size_mb: str | None


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    """Result of atomically publishing a WAV file."""

    ok: bool
    message: str
    md5: str | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Result of saving a library or pre-trimmed WAV file."""

    ok: bool
    message: str
    saved_path: Path | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """Result of deleting a library chime."""

    ok: bool
    message: str
    was_active: bool


@dataclass(frozen=True, slots=True)
class ChimeInfo:
    """Metadata about a chime in the uploaded library."""

    name: str
    size_bytes: int
    mtime_iso: str
    md5: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class _Strategy:
    name: str
    args: tuple[str, ...]
    trim: bool = False


_REENCODE_STRATEGIES: Final[tuple[_Strategy, ...]] = (
    _Strategy(
        name="Standard (16-bit, 44.1kHz, mono)",
        args=("-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1"),
    ),
    _Strategy(
        name="Trimmed (16-bit, 44.1kHz, mono)",
        args=("-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1"),
        trim=True,
    ),
)


class FileStorageLike(Protocol):
    """Minimal file-upload protocol required by the service layer."""

    filename: str | None

    def save(self, _dst: str | Path) -> None: ...

    def read(self, _size: int = -1) -> bytes: ...


class _SubprocessRunBytes(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]: ...


class _SubprocessRunText(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]: ...


def _file_md5(file_path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_MD5_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


def _safe_name(name: str | None) -> str:
    return secure_filename(name or "")


def _validate_library_name(name: str) -> None:
    if any(token in name for token in _PATH_TRAVERSAL_FORBIDDEN):
        msg = f"Invalid chime filename: {name!r}"
        raise ValueError(msg)


def _trim_duration_seconds() -> float:
    max_bytes = _MAX_LOCK_CHIME_SIZE_BYTES - _TRIM_HEADER_OVERHEAD_BYTES
    bytes_per_second = _WAV_TARGET_SAMPLE_RATE * _PCM_BYTES_PER_SAMPLE * _PCM_MONO_CHANNELS
    return max_bytes / bytes_per_second


def _decode_stderr(stderr: bytes | str) -> str:
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="ignore")
    return stderr


def _summarise_ffmpeg_error(stderr: bytes | str) -> str:
    stderr_text = _decode_stderr(stderr)
    error_lines = [
        line.strip()
        for line in stderr_text.splitlines()
        if line.strip()
        and any(
            keyword in line.lower()
            for keyword in ("error", "invalid", "could not", "failed", "unable")
        )
    ]
    if error_lines:
        relevant = error_lines[-_FFMPEG_ERROR_LINES:]
    else:
        non_empty = [line.strip() for line in stderr_text.splitlines() if line.strip()]
        relevant = non_empty[-_FFMPEG_ERROR_LINES:]
    return ". ".join(relevant)[:_FFMPEG_ERROR_MAX_CHARS] or "Unknown FFmpeg error"


def _ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise FileNotFoundError("ffmpeg")
    return executable


def _build_reencode_command(input_path: Path, output_path: Path, strategy: _Strategy) -> list[str]:
    command = [_ffmpeg_executable(), "-i", str(input_path)]
    if strategy.trim:
        command.extend(["-t", f"{_trim_duration_seconds():.1f}"])
    command.extend([*strategy.args, "-y", str(output_path)])
    return command


def _run_ffmpeg(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    # Fixed ffmpeg argv; shell is disabled and callers do not inject command text.
    run_bytes = cast("_SubprocessRunBytes", subprocess.__dict__["run"])
    return run_bytes(
        command,
        check=False,
        capture_output=True,
        timeout=_FFMPEG_TIMEOUT_SEC,
    )


def _run_ffmpeg_text(command: list[str]) -> subprocess.CompletedProcess[str]:
    # Fixed ffmpeg argv; shell is disabled and callers do not inject command text.
    run_text = cast("_SubprocessRunText", subprocess.__dict__["run"])
    return run_text(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=_FFMPEG_TIMEOUT_SEC,
    )


def _ensure_regular_file(path: Path, *, label: str) -> None:
    if not path.exists():
        msg = f"{label} not found: {path.name}"
        raise ValueError(msg)
    if path.is_symlink() or not path.is_file():
        msg = f"{label} is not a regular file: {path.name}"
        raise ValueError(msg)


def _active_path_from_chimes_dir(chimes_dir: Path) -> Path:
    return chimes_dir.parent / _LOCK_CHIME_FILENAME_DEFAULT


def _paths_refer_to_same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left.resolve(strict=False) == right.resolve(strict=False)


def _is_active_file(candidate: Path, active_path: Path, *, active_md5: str | None) -> bool:
    if active_path.exists() and _paths_refer_to_same_file(candidate, active_path):
        return True
    if active_md5 is None:
        return False
    return _file_md5(candidate) == active_md5


def _write_file_atomically(raw_bytes: bytes, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(".wav.tmp")
    try:
        with temp_path.open("wb") as fh:
            fh.write(raw_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        temp_path.replace(output_path)
    except OSError as exc:
        _safe_unlink(temp_path)
        msg = f"Failed to write {output_path}: {exc}"
        raise LockChimeFileError(msg) from exc
    return len(raw_bytes)


def _validate_wav_size(size_bytes: int) -> str | None:
    if size_bytes > _MAX_LOCK_CHIME_SIZE_BYTES:
        size_mb = size_bytes / _BYTES_PER_MIB
        return f"File is {size_mb:.2f} MB. Tesla requires lock chimes to be under 1 MB."
    if size_bytes == _OUTPUT_FILE_MISSING_BYTES:
        return "File is empty."
    return None


def _validate_wav_params(params: wave._wave_params) -> str | None:
    if params.sampwidth * 8 != _WAV_TARGET_BITS:
        return f"File is {params.sampwidth * 8}-bit. Tesla requires {_WAV_TARGET_BITS}-bit PCM."
    if params.framerate not in _WAV_REQUIRED_SAMPLE_RATES:
        return f"Sample rate is {params.framerate / 1000:.1f} kHz. Tesla requires 44.1 or 48 kHz."
    if params.comptype != "NONE":
        return f"File uses {params.comptype} compression. Tesla requires uncompressed PCM."
    if params.nchannels not in _WAV_REQUIRED_CHANNELS:
        return f"File has {params.nchannels} channels. Tesla requires mono or stereo."
    return None


def validate_tesla_wav(path: str | Path) -> WavValidation:
    """Validate a WAV file against Tesla lock-chime requirements.

    Args:
        path: WAV file to inspect.

    Returns:
        Validation outcome with the v1-compatible message text.
    """
    wav_path = Path(path)
    try:
        size_message = _validate_wav_size(wav_path.stat().st_size)
        if size_message is not None:
            return WavValidation(ok=False, message=size_message)
        with contextlib.closing(wave.open(str(wav_path), "rb")) as wav_file:
            params = wav_file.getparams()
    except (wave.Error, EOFError):
        return WavValidation(ok=False, message="Not a valid WAV file.")
    except OSError as exc:
        return WavValidation(ok=False, message=f"Unable to read file: {exc}")
    params_message = _validate_wav_params(params)
    if params_message is not None:
        return WavValidation(ok=False, message=params_message)
    return WavValidation(ok=True, message="Valid")


def _notify_progress(
    progress_callback: _ProgressCallback | None,
    attempt: int,
    strategy: _Strategy,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        f"{_PROGRESS_ATTEMPT_PREFIX} {attempt}/{len(_REENCODE_STRATEGIES)}: {strategy.name}"
    )


def _check_reencode_output(target: Path, attempt: int) -> ReencodeResult | str:
    if not target.exists() or target.stat().st_size == _OUTPUT_FILE_MISSING_BYTES:
        return "Re-encoding produced an empty file"
    size_bytes = target.stat().st_size
    size_mb = size_bytes / _BYTES_PER_MIB
    if size_bytes > _MAX_LOCK_CHIME_SIZE_BYTES:
        if attempt == len(_REENCODE_STRATEGIES):
            return ReencodeResult(
                ok=False,
                message=(
                    "Unable to fit file under 1 MB even after trimming. "
                    f"Final size: {size_mb:.2f} MB."
                ),
                strategy=None,
                attempt=None,
                size_mb=None,
            )
        return f"File still too large: {size_mb:.2f} MB (need < 1 MB)"
    return ReencodeResult(
        ok=True,
        message="",
        strategy="",
        attempt=attempt,
        size_mb=f"{size_mb:.2f}",
    )


def reencode_wav_for_tesla(
    input_path: str | Path,
    output_path: str | Path,
    progress_callback: _ProgressCallback | None = None,
) -> ReencodeResult:
    """Re-encode a WAV file into Tesla's supported PCM profile.

    Args:
        input_path: Source audio file.
        output_path: Destination file written by FFmpeg.
        progress_callback: Optional callback for attempt updates.

    Returns:
        Outcome of the multi-strategy encode attempt.
    """
    source = Path(input_path)
    target = Path(output_path)
    last_error = "Unknown FFmpeg error"
    for attempt, strategy in enumerate(_REENCODE_STRATEGIES, start=1):
        try:
            _notify_progress(progress_callback, attempt, strategy)
            result = _run_ffmpeg(_build_reencode_command(source, target, strategy))
            if result.returncode != 0:
                last_error = f"FFmpeg conversion failed: {_summarise_ffmpeg_error(result.stderr)}"
                continue
            output_status = _check_reencode_output(target, attempt)
            if isinstance(output_status, str):
                last_error = output_status
                continue
            if not output_status.ok:
                return output_status
            return ReencodeResult(
                ok=True,
                message=(
                    f"Successfully re-encoded using {strategy.name} "
                    f"(size: {output_status.size_mb} MB)"
                ),
                strategy=strategy.name,
                attempt=attempt,
                size_mb=output_status.size_mb,
            )
        except FileNotFoundError:
            return ReencodeResult(
                ok=False,
                message="FFmpeg is not installed on the system",
                strategy=None,
                attempt=None,
                size_mb=None,
            )
        except subprocess.TimeoutExpired:
            last_error = "Re-encoding timed out (file too large or complex)"
        except Exception as exc:  # pragma: no cover - top-level fault isolation.
            logger.exception("Unexpected error while re-encoding %s", source)
            last_error = f"Re-encoding error: {exc}"
    return ReencodeResult(
        ok=False,
        message=f"All re-encoding attempts failed. Last error: {last_error}",
        strategy=None,
        attempt=None,
        size_mb=None,
    )


def _require_loudnorm_json(stderr_text: str) -> dict[str, object]:
    json_start = stderr_text.rfind("{")
    if json_start == -1:
        raise LockChimeAudioError("Could not find loudness analysis data in FFmpeg output")
    try:
        raw_stats = json.loads(stderr_text[json_start:])
    except json.JSONDecodeError as exc:
        logger.exception("Failed to parse FFmpeg loudnorm stats")
        raise LockChimeAudioError("Failed to analyze audio loudness") from exc
    if not isinstance(raw_stats, dict):
        raise LockChimeAudioError("FFmpeg loudnorm analysis was not a JSON object")
    return {str(key): value for key, value in raw_stats.items()}


def _required_stat(stats: dict[str, object], key: str) -> str:
    if key not in stats:
        raise LockChimeAudioError(f"Missing loudnorm field: {key}")
    value = stats[key]
    if not isinstance(value, str):
        raise LockChimeAudioError(f"Invalid loudnorm field type for {key}: {type(value).__name__}")
    return value


def normalize_audio(input_path: str | Path, target_lufs: int = _TARGET_LUFS_DEFAULT) -> Path:
    """Normalize a WAV file with FFmpeg loudnorm and return a temp output path.

    Caller owns cleanup of the returned file.

    Args:
        input_path: Source WAV file.
        target_lufs: Target loudness in LUFS.

    Returns:
        Path to the normalized temporary WAV file.

    Raises:
        LockChimeAudioError: If FFmpeg is unavailable, times out, returns invalid
            stats, or fails to write a normalized WAV.
    """
    source = Path(input_path)
    with tempfile.NamedTemporaryFile(suffix=_WAV_SUFFIX, delete=False) as temp_file:
        temp_path = Path(temp_file.name)
    try:
        analysis = _run_ffmpeg_text(
            [
                _ffmpeg_executable(),
                "-i",
                str(source),
                "-af",
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ]
        )
        stats = _require_loudnorm_json(analysis.stderr)
        result = _run_ffmpeg_text(
            [
                _ffmpeg_executable(),
                "-i",
                str(source),
                "-af",
                (
                    f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
                    f"measured_I={_required_stat(stats, 'input_i')}:"
                    f"measured_LRA={_required_stat(stats, 'input_lra')}:"
                    f"measured_TP={_required_stat(stats, 'input_tp')}:"
                    f"measured_thresh={_required_stat(stats, 'input_thresh')}:"
                    f"offset={_required_stat(stats, 'target_offset')}"
                ),
                "-ar",
                str(_WAV_TARGET_SAMPLE_RATE),
                "-y",
                str(temp_path),
            ]
        )
        if result.returncode != 0:
            raise LockChimeAudioError(
                f"Audio normalization failed: {_summarise_ffmpeg_error(result.stderr)}"
            )
        if not temp_path.exists() or temp_path.stat().st_size == _OUTPUT_FILE_MISSING_BYTES:
            raise LockChimeAudioError("Normalization produced empty file")
    except FileNotFoundError as exc:
        _safe_unlink(temp_path)
        raise LockChimeAudioError("FFmpeg is not installed on the system") from exc
    except subprocess.TimeoutExpired as exc:
        _safe_unlink(temp_path)
        raise LockChimeAudioError("Audio normalization timed out") from exc
    except LockChimeAudioError:
        _safe_unlink(temp_path)
        raise
    except OSError as exc:
        _safe_unlink(temp_path)
        raise LockChimeAudioError(f"Audio normalization failed: {exc}") from exc
    except Exception as exc:  # pragma: no cover - top-level fault isolation.
        _safe_unlink(temp_path)
        logger.exception("Unexpected error while normalizing %s", source)
        raise LockChimeAudioError(f"Audio normalization failed: {exc}") from exc
    return temp_path


def replace_lock_chime(source_path: str | Path, target_path: str | Path) -> ReplaceResult:
    """Atomically replace the active Tesla-visible lock chime.

    Args:
        source_path: Source WAV to publish.
        target_path: Active ``LockChime.wav`` location.

    Returns:
        Replacement result with MD5 and final size.

    Raises:
        ValueError: If the source file is empty.
        LockChimeFileError: If the copy or atomic replace fails.
    """
    source = Path(source_path)
    target = Path(target_path)
    size_bytes = source.stat().st_size
    if size_bytes == _OUTPUT_FILE_MISSING_BYTES:
        raise ValueError("Selected WAV file is empty.")
    temp_path = target.with_suffix(".wav.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as src_fh, temp_path.open("wb") as tmp_fh:
            shutil.copyfileobj(src_fh, tmp_fh, length=_MD5_CHUNK_BYTES)
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        temp_path.replace(target)
    except OSError as exc:
        _safe_unlink(temp_path)
        msg = f"Failed to replace active chime: {exc}"
        raise LockChimeFileError(msg) from exc
    md5 = _file_md5(target)
    return ReplaceResult(
        ok=True,
        message=f"Successfully replaced active lock chime with {source.name}",
        md5=md5,
        size_bytes=target.stat().st_size,
    )


def set_active_chime(name: str, chimes_dir: str | Path, active_path: str | Path) -> ReplaceResult:
    """Publish a library chime as the active Tesla-visible lock chime.

    Args:
        name: Filename inside ``chimes_dir``.
        chimes_dir: Library directory.
        active_path: Active ``LockChime.wav`` path.

    Returns:
        Result from the atomic replace operation.

    Raises:
        ValueError: If the filename is unsafe, missing, non-regular, or not a
            Tesla-compatible WAV.
        LockChimeFileError: If the atomic publish fails.
    """
    _validate_library_name(name)
    source = Path(chimes_dir) / name
    _ensure_regular_file(source, label="Chime file")
    validation = validate_tesla_wav(source)
    if not validation.ok:
        raise ValueError(f"Invalid chime file: {validation.message}")
    return replace_lock_chime(source, active_path)


def upload_chime_file(
    file_storage: FileStorageLike,
    chimes_dir: str | Path,
    max_size: int,
) -> UploadResult:
    """Save an uploaded WAV file into the chime library.

    Args:
        file_storage: Werkzeug-like upload object.
        chimes_dir: Destination library directory.
        max_size: Maximum allowed file size in bytes.

    Returns:
        Upload outcome and saved path when successful.

    Raises:
        LockChimeFileError: If the filesystem write or atomic move fails.
    """
    safe_name = _safe_name(file_storage.filename)
    if not safe_name:
        return UploadResult(
            ok=False,
            message="Filename is required.",
            saved_path=None,
            size_bytes=None,
        )
    if not safe_name.lower().endswith(_WAV_SUFFIX):
        return UploadResult(
            ok=False,
            message="Filename must end with .wav",
            saved_path=None,
            size_bytes=None,
        )
    if safe_name.lower() == _LOCK_CHIME_FILENAME_DEFAULT.lower():
        return UploadResult(
            ok=False,
            message="Cannot upload a file named LockChime.wav. Please rename your file.",
            saved_path=None,
            size_bytes=None,
        )
    library_dir = Path(chimes_dir)
    library_dir.mkdir(parents=True, exist_ok=True)
    final_path = library_dir / safe_name
    temp_path = final_path.with_suffix(".wav.tmp")
    try:
        file_storage.save(temp_path)
        size_bytes = temp_path.stat().st_size
        if size_bytes > max_size:
            _safe_unlink(temp_path)
            size_mb = size_bytes / _BYTES_PER_MIB
            return UploadResult(
                ok=False,
                message=f"File is {size_mb:.2f} MB. Limit is {max_size / _BYTES_PER_MIB:.2f} MB.",
                saved_path=None,
                size_bytes=size_bytes,
            )
        validation = validate_tesla_wav(temp_path)
        if not validation.ok:
            _safe_unlink(temp_path)
            return UploadResult(
                ok=False,
                message=f"Upload failed: {validation.message}",
                saved_path=None,
                size_bytes=size_bytes,
            )
        temp_path.replace(final_path)
    except OSError as exc:
        _safe_unlink(temp_path)
        raise LockChimeFileError(f"Failed to save uploaded chime: {exc}") from exc
    return UploadResult(
        ok=True,
        message=f"Successfully uploaded {safe_name}",
        saved_path=final_path,
        size_bytes=final_path.stat().st_size,
    )


def save_pretrimmed_wav(raw_bytes: bytes, output_path: str | Path) -> UploadResult:
    """Persist a browser-trimmed WAV blob after lightweight validation.

    Args:
        raw_bytes: Browser-produced WAV bytes.
        output_path: Destination file path.

    Returns:
        Upload result describing the saved file.

    Raises:
        LockChimeFileError: If the atomic write fails.
    """
    if len(raw_bytes) < _WAV_HEADER_MIN_BYTES:
        return UploadResult(
            ok=False,
            message="Not a valid WAV file.",
            saved_path=None,
            size_bytes=None,
        )
    if not raw_bytes.startswith(_RIFF_MAGIC) or raw_bytes[8:_WAV_HEADER_MIN_BYTES] != _WAVE_MAGIC:
        return UploadResult(
            ok=False,
            message="Not a valid WAV file.",
            saved_path=None,
            size_bytes=None,
        )
    output = Path(output_path)
    size_bytes = _write_file_atomically(raw_bytes, output)
    validation = validate_tesla_wav(output)
    if not validation.ok:
        _safe_unlink(output)
        return UploadResult(
            ok=False,
            message=f"Pre-trimmed file validation failed: {validation.message}",
            saved_path=None,
            size_bytes=size_bytes,
        )
    return UploadResult(
        ok=True,
        message=f"Successfully uploaded {output.name}",
        saved_path=output,
        size_bytes=size_bytes,
    )


def delete_chime_file(name: str, chimes_dir: str | Path, active_path: str | Path) -> DeleteResult:
    """Delete a library chime and report whether it was the active one.

    Args:
        name: Filename inside ``chimes_dir``.
        chimes_dir: Chime library directory.
        active_path: Active ``LockChime.wav`` path or symlink.

    Returns:
        Delete outcome and a flag for caller-side fallback handling.

    Raises:
        ValueError: If the filename is unsafe, missing, or non-regular.
        LockChimeFileError: If the unlink fails.
    """
    _validate_library_name(name)
    candidate = Path(chimes_dir) / name
    _ensure_regular_file(candidate, label="Chime file")
    resolved_active_path = Path(active_path)
    was_active = resolved_active_path.exists() and _paths_refer_to_same_file(
        candidate,
        resolved_active_path,
    )
    try:
        candidate.unlink()
    except OSError as exc:
        raise LockChimeFileError(f"Failed to delete chime: {exc}") from exc
    return DeleteResult(ok=True, message=f"Successfully deleted {name}", was_active=was_active)


def list_chime_files(chimes_dir: str | Path) -> tuple[ChimeInfo, ...]:
    """List uploaded ``.wav`` chimes sorted alphabetically.

    Args:
        chimes_dir: Chime library directory.

    Returns:
        Tuple of chime metadata. Non-WAV files are skipped silently.

    Raises:
        LockChimeFileError: If directory scanning fails.
    """
    library_dir = Path(chimes_dir)
    if not library_dir.exists():
        return ()
    active_path = _active_path_from_chimes_dir(library_dir)
    active_md5 = _file_md5(active_path) if active_path.exists() and active_path.is_file() else None
    try:
        infos: list[ChimeInfo] = []
        for candidate in sorted(library_dir.iterdir(), key=lambda path: path.name.lower()):
            if candidate.suffix.lower() != _WAV_SUFFIX:
                continue
            if candidate.is_symlink() or not candidate.is_file():
                continue
            md5 = _file_md5(candidate)
            infos.append(
                ChimeInfo(
                    name=candidate.name,
                    size_bytes=candidate.stat().st_size,
                    mtime_iso=datetime.fromtimestamp(
                        candidate.stat().st_mtime,
                        tz=UTC,
                    ).isoformat(),
                    md5=md5,
                    is_active=_is_active_file(candidate, active_path, active_md5=active_md5),
                )
            )
    except OSError as exc:
        raise LockChimeFileError(f"Failed to list chime files: {exc}") from exc
    return tuple(infos)


__all__ = (
    "ChimeInfo",
    "DeleteResult",
    "FileStorageLike",
    "LockChimeAudioError",
    "LockChimeFileError",
    "ReencodeResult",
    "ReplaceResult",
    "UploadResult",
    "WavValidation",
    "delete_chime_file",
    "list_chime_files",
    "normalize_audio",
    "reencode_wav_for_tesla",
    "replace_lock_chime",
    "save_pretrimmed_wav",
    "set_active_chime",
    "upload_chime_file",
    "validate_tesla_wav",
)
