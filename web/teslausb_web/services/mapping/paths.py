from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sei import SeiParserProtocol, SeiSidecarProtocol

logger = logging.getLogger(__name__)

_CLOCK_SKEW_WARN_SECONDS = 300
_TIMESTAMP_PREFIX_LEN = 19


def _timestamp_from_filename(filename: str) -> str | None:
    base = Path(filename).name
    if len(base) < _TIMESTAMP_PREFIX_LEN or base[4] != "-" or base[10] != "_":
        return None
    candidate = f"{base[:10]}T{base[11:_TIMESTAMP_PREFIX_LEN].replace('-', ':')}+00:00"
    try:
        return datetime.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _resolve_recording_time(
    video_path: Path,
    *,
    parser: SeiParserProtocol,
    sidecar: SeiSidecarProtocol | None = None,
) -> str | None:
    filename_timestamp = _timestamp_from_filename(video_path.name)
    mvhd_utc = _mvhd_creation_time(video_path, parser=parser, sidecar=sidecar)
    if mvhd_utc is None:
        return filename_timestamp
    try:
        local_time = datetime.fromtimestamp(mvhd_utc.timestamp(), tz=UTC).astimezone()
    except (OverflowError, OSError, ValueError):
        return filename_timestamp
    _warn_on_clock_skew(video_path.name, filename_timestamp, local_time)
    return local_time.isoformat()


def _mvhd_creation_time(
    video_path: Path,
    *,
    parser: SeiParserProtocol,
    sidecar: SeiSidecarProtocol | None,
) -> datetime | None:
    if sidecar is None:
        sidecar = _read_sidecar(video_path, parser)
    if sidecar is not None and sidecar.mvhd_creation_time_utc is not None:
        return sidecar.mvhd_creation_time_utc
    try:
        return parser.extract_mvhd_creation_time(video_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mvhd read failed for %s: %s", video_path, exc)
        return None


def _read_sidecar(video_path: Path, parser: SeiParserProtocol) -> SeiSidecarProtocol | None:
    try:
        return parser.read_sei_sidecar(video_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sidecar read failed for %s: %s", video_path, exc)
        return None


def _warn_on_clock_skew(
    filename: str,
    filename_timestamp: str | None,
    local_naive: datetime,
) -> None:
    if filename_timestamp is None:
        return
    try:
        skew = abs((datetime.fromisoformat(filename_timestamp) - local_naive).total_seconds())
    except ValueError:
        return
    if skew < _CLOCK_SKEW_WARN_SECONDS:
        return
    logger.warning(
        "Tesla onboard-clock skew detected for %s: filename says %s, mvhd UTC says %s; using mvhd",
        filename,
        filename_timestamp,
        local_naive.isoformat(),
    )


def canonical_key(video_path: str | Path) -> str:
    norm = str(video_path).replace("\\", "/")
    basename = norm.rsplit("/", 1)[-1]
    parts = [part for part in norm.split("/") if part]
    for index, part in enumerate(parts):
        if part in {"SavedClips", "SentryClips"} and index + 2 < len(parts):
            return f"{part}/{parts[index + 1]}/{basename}"
    return basename


def candidate_db_paths(canonical_key_value: str) -> tuple[str, ...]:
    if "/" in canonical_key_value:
        return (canonical_key_value,)
    return (
        canonical_key_value,
        f"RecentClips/{canonical_key_value}",
    )


def relative_video_path(
    video_path: Path,
    *,
    media_root: Path,
) -> str:
    try:
        return video_path.relative_to(media_root).as_posix()
    except ValueError:
        return video_path.name
