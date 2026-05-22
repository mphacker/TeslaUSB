from __future__ import annotations

import struct
from numbers import Real
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from .discovery import _find_front_camera_videos
from .sei import SeiParserProtocol, get_sei_parser
from .service import DiagnoseError

if TYPE_CHECKING:
    from .service import MappingService

_MAX_DIAGNOSE_SIZE_BYTES = 150 * 1024 * 1024
_MIN_VIDEO_BYTES = 8
_MAX_NAL_SCAN = 5_000
_MDAT_PREFIX_BYTES = 4


def diagnose_video(
    service: MappingService,
    *,
    teslacam_path: Path,
    max_videos: int = 3,
) -> dict[str, object]:
    parser = get_sei_parser(service._parser_or_default())
    result: dict[str, object] = {
        "teslacam_path": str(teslacam_path),
        "path_exists": teslacam_path.is_dir(),
        "videos": [],
        "summary": "",
    }
    if not teslacam_path.is_dir():
        result["summary"] = f"TeslaCam path does not exist: {teslacam_path}"
        return result
    videos = list(_find_front_camera_videos(teslacam_path))
    result["total_front_videos"] = len(videos)
    diagnostics = [_diagnose_single_video(path, parser) for path in videos[:max_videos]]
    result["videos"] = diagnostics
    gps_found = sum(1 for item in diagnostics if _gps_count(item) > 0)
    result["summary"] = (
        f"{len(videos)} front-camera videos found, "
        f"{len(diagnostics)} tested: {gps_found} have GPS data"
    )
    return result


def _diagnose_single_video(path: Path, parser: SeiParserProtocol) -> dict[str, object]:
    try:
        stat_result = path.stat()
    except OSError as exc:
        return {"path": str(path), "error": str(exc)}
    diag: dict[str, object] = {
        "path": str(path),
        "file_size": stat_result.st_size,
        "file_size_mb": round(stat_result.st_size / 1024.0 / 1024.0, 2),
    }
    if stat_result.st_size < _MIN_VIDEO_BYTES:
        diag["error"] = "File too small"
        return diag
    with path.open("rb") as handle:
        header = handle.read(min(32, stat_result.st_size))
    diag["first_16_bytes_hex"] = header[:16].hex()
    diag["has_ftyp"] = b"ftyp" in header[:12]
    if not diag["has_ftyp"]:
        diag["error"] = "Not a valid MP4 (no ftyp box in first 12 bytes)"
        return diag
    diag.update(_diagnose_nal_structure(path))
    sampled = list(parser.extract_sei_messages(path, sample_rate=1))[:10]
    diag["sei_messages_sampled"] = len(sampled)
    gps_messages = [message for message in sampled if bool(getattr(message, "has_gps", False))]
    diag["gps_messages"] = len(gps_messages)
    if gps_messages:
        diag["sample_gps"] = _sample_gps_payload(gps_messages[0])
    elif sampled:
        diag["sample_sei_no_gps"] = _sample_non_gps_payload(sampled[0])
    return diag


def _diagnose_nal_structure(video_path: Path) -> dict[str, object]:
    file_size = video_path.stat().st_size
    if file_size > _MAX_DIAGNOSE_SIZE_BYTES:
        raise DiagnoseError(f"File too large for diagnosis ({file_size} bytes)")
    with video_path.open("rb") as handle:
        data = handle.read()
    mdat = _find_mdat_bounds(data)
    if mdat is None:
        return {"nal_error": "No mdat box found"}
    start, end = mdat
    cursor = start
    nal_types: dict[str, int] = {}
    nal_count = 0
    while cursor + _MDAT_PREFIX_BYTES <= end and nal_count < _MAX_NAL_SCAN:
        nal_size = struct.unpack(">I", data[cursor : cursor + 4])[0]
        cursor += 4
        if nal_size < 1 or cursor + nal_size > len(data):
            break
        nal_type = data[cursor] & 0x1F
        key = str(nal_type)
        nal_types[key] = nal_types.get(key, 0) + 1
        nal_count += 1
        cursor += nal_size
    return {
        "mdat_size": end - start,
        "nal_count": nal_count,
        "nal_types": nal_types,
        "sei_type6_count": nal_types.get("6", 0),
    }


def _find_mdat_bounds(data: bytes) -> tuple[int, int] | None:
    marker = data.find(b"mdat")
    if marker < _MDAT_PREFIX_BYTES:
        return None
    size = struct.unpack(">I", data[marker - _MDAT_PREFIX_BYTES : marker])[0]
    start = marker + 4
    end = min(len(data), marker - 4 + size)
    if end <= start:
        return None
    return start, end


def _sample_gps_payload(message: object) -> dict[str, object]:
    return {
        "lat": getattr(message, "latitude_deg", 0.0),
        "lon": getattr(message, "longitude_deg", 0.0),
        "speed_mph": round(float(getattr(message, "speed_mph", 0.0)), 1),
        "heading": getattr(message, "heading_deg", None),
        "gear": getattr(message, "gear_state", None),
    }


def _sample_non_gps_payload(message: object) -> dict[str, object]:
    return {
        "lat": getattr(message, "latitude_deg", 0.0),
        "lon": getattr(message, "longitude_deg", 0.0),
        "speed_mph": round(float(getattr(message, "speed_mph", 0.0)), 1),
        "frame": getattr(message, "frame_index", 0),
    }


def _gps_count(payload: dict[str, object]) -> int:
    value = payload.get("gps_messages", 0)
    return int(float(value)) if isinstance(value, Real) else 0
