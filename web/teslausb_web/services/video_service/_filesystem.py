"""Pure filesystem helpers: scan, parse, mtime, MP4 header probe.

No Flask import, no I/O orchestration beyond the FS itself. Each
helper is independently unit-testable against ``tmp_path``.

The Tesla camera filename convention is::

    YYYY-MM-DD_HH-MM-SS-{camera}.mp4

where ``{camera}`` is one of ``front``, ``back``, ``left_repeater``,
``right_repeater``, ``left_pillar``, ``right_pillar``. Two special
filenames also appear: ``event.mp4`` (the synthesised grid view that
Tesla writes for SavedClips/SentryClips events) and ``event.json``
(metadata: city, reason, timestamp, est_lat, est_lon).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from teslausb_web.services.video_service._models import (
    CAMERA_KEYS,
    CameraVideos,
    Clip,
    ClipFile,
    EncryptedFlags,
    EventDetails,
    EventSummary,
    SessionGroup,
)

logger = logging.getLogger(__name__)

# MP4 ``ftyp`` box magic — required by valid MP4. Encrypted-by-Tesla
# clips lack this in the first 12 bytes.
_MP4_FTYP_SIGNATURE: Final[bytes] = b"ftyp"
_MP4_PROBE_BYTES: Final[int] = 12
_BYTES_PER_MIB: Final[int] = 1024 * 1024
_VIDEO_EXTENSIONS: Final[tuple[str, ...]] = (".mp4",)
_EVENT_NAME_FORMAT: Final[str] = "%Y-%m-%d_%H-%M-%S"
_DATETIME_DISPLAY: Final[str] = "%Y-%m-%d %I:%M:%S %p"
_SPLIT_PARTS_EXPECTED: Final[int] = 2


def is_valid_mp4(path: Path) -> bool:
    """Return ``True`` iff the first 12 bytes contain the ``ftyp`` box.

    Tesla emits encrypted-at-rest clips in RecentClips until the user
    saves them; those files are 100% the right size but the header
    is shredded. Probing 12 bytes per file is the cheapest possible
    encryption-vs-real check.
    """
    try:
        with path.open("rb") as fp:
            header = fp.read(_MP4_PROBE_BYTES)
    except OSError as exc:
        logger.debug("is_valid_mp4: cannot read %s: %s", path, exc)
        return False
    if len(header) < _MP4_PROBE_BYTES:
        return False
    return _MP4_FTYP_SIGNATURE in header


def camera_key_for_filename(name: str) -> str | None:
    """Return the canonical camera key for a TeslaCam clip filename.

    Returns ``None`` for non-camera files (``event.mp4``,
    ``thumb.png``, etc).
    """
    lowered = name.lower()
    if lowered == "event.mp4":
        return "event"
    # Match longest first: ``left_repeater`` must beat ``left``.
    for key in CAMERA_KEYS:
        if key in lowered:
            return key
    return None


def list_event_folders(folder_path: Path) -> list[tuple[str, Path, float]]:
    """List immediate subdirectories of ``folder_path``.

    Returns ``(name, path, mtime)`` tuples sorted newest-first.
    """
    out: list[tuple[str, Path, float]] = []
    try:
        with os.scandir(folder_path) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                out.append((entry.name, Path(entry.path), mtime))
    except OSError as exc:
        logger.debug("list_event_folders: %s: %s", folder_path, exc)
        return []
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def parse_event_lightweight(event_path: Path, event_name: str) -> EventSummary | None:
    """Build an :class:`EventSummary` without probing MP4 headers.

    Used by the paginated list view. Reading event.json is cheap; we
    skip the per-camera ``is_valid_mp4`` probe because it would
    dominate the request time for a SentryClips folder with 1000+
    events.
    """
    try:
        metadata = _read_event_json(event_path)
        camera_videos, total_size, latest_mtime = _scan_camera_videos(event_path)
        if not camera_videos.any_present():
            return None
        timestamp = _event_timestamp(event_name, latest_mtime)
        return EventSummary(
            name=event_name,
            timestamp=timestamp,
            datetime_str=datetime.fromtimestamp(timestamp, tz=UTC).strftime(_DATETIME_DISPLAY),
            size_mb=round(total_size / _BYTES_PER_MIB, 2),
            camera_videos=camera_videos,
            city=str(metadata.get("city", "") or ""),
            reason=str(metadata.get("reason", "") or ""),
        )
    except OSError as exc:
        logger.debug("parse_event_lightweight: %s: %s", event_path, exc)
        return None


def parse_event_full(event_path: Path, event_name: str) -> EventDetails | None:
    """Build a full :class:`EventDetails` including encrypted-flag probe."""
    try:
        metadata = _read_event_json(event_path)
        camera_videos, encrypted, total_size, latest_mtime = _scan_camera_videos_with_encryption(
            event_path
        )
        clips = _parse_clips(event_path)
        if not camera_videos.any_present() and not clips:
            return None
        timestamp = _event_timestamp(event_name, latest_mtime)
        starting_clip_index = _pick_starting_clip(clips, timestamp)
        return EventDetails(
            name=event_name,
            path=str(event_path),
            timestamp=timestamp,
            datetime_str=datetime.fromtimestamp(timestamp, tz=UTC).strftime(_DATETIME_DISPLAY),
            size_bytes=total_size,
            size_mb=round(total_size / _BYTES_PER_MIB, 2),
            camera_videos=camera_videos,
            encrypted_videos=encrypted,
            metadata=metadata,
            city=str(metadata.get("city", "") or ""),
            reason=str(metadata.get("reason", "") or ""),
            clips=clips,
            starting_clip_index=starting_clip_index,
        )
    except OSError as exc:
        logger.debug("parse_event_full: %s: %s", event_path, exc)
        return None


def group_flat_sessions(
    folder_path: Path, page: int, per_page: int
) -> tuple[list[SessionGroup], int]:
    """Group flat-folder clips by session (RecentClips / ArchivedClips).

    Two passes:

    1. Scan to bucket files into sessions (timestamp prefix). Track
       the newest mtime per session for sort ordering. No size or
       header probes yet — keeps the global pass cheap.
    2. For the page slice, accumulate size + camera bucket + encrypted
       flag. We only open file headers for the page being rendered.
    """
    session_timestamps, session_files = _bucket_flat_sessions(folder_path)
    total_count = len(session_timestamps)
    if not session_timestamps:
        return [], 0

    sorted_sessions = sorted(session_timestamps.items(), key=lambda kv: kv[1], reverse=True)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    paged_ids = [sid for sid, _ in sorted_sessions[start_idx:end_idx]]
    if not paged_ids:
        return [], total_count

    result: list[SessionGroup] = []
    for sid in paged_ids:
        files = session_files[sid]
        size = sum(f.size_bytes for f in files)
        cameras = dict.fromkeys(CAMERA_KEYS)
        encrypted = dict.fromkeys(CAMERA_KEYS, False)
        for f in files:
            cam = camera_key_for_filename(f.name)
            if cam is None or cam == "event":
                continue
            if cameras[cam] is None:
                cameras[cam] = f.name
                if not is_valid_mp4(Path(f.path)):
                    encrypted[cam] = True
        ts = session_timestamps[sid]
        result.append(
            SessionGroup(
                name=sid,
                timestamp=ts,
                datetime_str=datetime.fromtimestamp(ts, tz=UTC).strftime(_DATETIME_DISPLAY),
                size_mb=round(size / _BYTES_PER_MIB, 2),
                camera_videos=CameraVideos(**cameras),
                encrypted_videos=EncryptedFlags(**encrypted),
            )
        )
    return result, total_count


def get_session_files(folder_path: Path, session_id: str) -> list[ClipFile]:
    """Return all clip files matching ``session_id`` in a flat folder."""
    out: list[ClipFile] = []
    try:
        with os.scandir(folder_path) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.lower().endswith(_VIDEO_EXTENSIONS):
                    continue
                if not entry.name.startswith(session_id):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                out.append(
                    ClipFile(
                        name=entry.name,
                        path=entry.path,
                        size_bytes=st.st_size,
                        mtime=st.st_mtime,
                    )
                )
    except OSError as exc:
        logger.debug("get_session_files: %s: %s", folder_path, exc)
        return []
    out.sort(key=lambda c: c.name)
    return out


def count_videos(folder_path: Path) -> int:
    """Recursively count ``*.mp4`` files in a folder (one level deep).

    Mirrors v1's two-level scan: walk immediate children, then
    contents of any subdirectory. Deeper nesting isn't valid for
    TeslaCam folders so we don't recurse further.
    """
    total = 0
    try:
        with os.scandir(folder_path) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    try:
                        with os.scandir(Path(entry.path)) as sub_entries:
                            for sub in sub_entries:
                                if sub.name.lower().endswith(_VIDEO_EXTENSIONS):
                                    total += 1
                    except OSError:
                        continue
                elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(
                    _VIDEO_EXTENSIONS
                ):
                    total += 1
    except OSError:
        return 0
    return total


# ---------------------------------------------------------------------------
# Private helpers — exported only via the wrappers above.


def _read_event_json(event_path: Path) -> dict[str, object]:
    candidate = event_path / "event.json"
    if not candidate.exists():
        return {}
    try:
        with candidate.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("_read_event_json: %s: %s", candidate, exc)
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _scan_camera_videos(
    event_path: Path,
) -> tuple[CameraVideos, int, float]:
    """Light scan: filenames + size + latest mtime, no header probe."""
    cameras: dict[str, str | None] = dict.fromkeys(CAMERA_KEYS)
    total = 0
    latest = 0.0
    with os.scandir(event_path) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            if not entry.name.lower().endswith(_VIDEO_EXTENSIONS):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            total += st.st_size
            latest = max(latest, st.st_mtime)
            cam = camera_key_for_filename(entry.name)
            if cam is None or cam == "event":
                continue
            if cameras[cam] is None:
                cameras[cam] = entry.name
    return CameraVideos(**cameras), total, latest


def _scan_camera_videos_with_encryption(
    event_path: Path,
) -> tuple[CameraVideos, EncryptedFlags, int, float]:
    """Full scan: includes the per-camera ``is_valid_mp4`` probe."""
    cameras: dict[str, str | None] = dict.fromkeys(CAMERA_KEYS)
    cameras_extra: dict[str, str | None] = {"event": None}
    encrypted: dict[str, bool] = dict.fromkeys(CAMERA_KEYS, False)
    total = 0
    latest = 0.0
    with os.scandir(event_path) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            if not entry.name.lower().endswith(_VIDEO_EXTENSIONS):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            total += st.st_size
            latest = max(latest, st.st_mtime)
            cam = camera_key_for_filename(entry.name)
            if cam is None:
                continue
            if cam == "event":
                if cameras_extra["event"] is None:
                    cameras_extra["event"] = entry.name
                continue
            if cameras[cam] is None:
                cameras[cam] = entry.name
                if not is_valid_mp4(Path(entry.path)):
                    encrypted[cam] = True
    merged = {**cameras, **cameras_extra}
    return (
        CameraVideos(**merged),
        EncryptedFlags(**encrypted),
        total,
        latest,
    )


def _parse_clips(event_path: Path) -> tuple[Clip, ...]:
    """Parse all clips in a SavedClips/SentryClips event folder.

    Each event folder may contain multiple 1-minute clip-sets, each
    with up to six camera angles sharing a timestamp prefix.
    """
    # Per-clip working state: timestamp + the two mutable dicts being
    # filled in as we scan. Typed explicitly so mypy doesn't need
    # ``assert isinstance`` rescue calls (charter §"no asserts in
    # non-test code").
    buckets: dict[str, tuple[float, dict[str, str | None], dict[str, bool]]] = {}
    try:
        with os.scandir(event_path) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                lower = entry.name.lower()
                if not lower.endswith(_VIDEO_EXTENSIONS):
                    continue
                if lower == "event.mp4":
                    continue
                ts_str, camera = _split_clip_filename(entry.name)
                if ts_str is None or camera is None:
                    continue
                if camera not in CAMERA_KEYS:
                    continue
                bucket = buckets.get(ts_str)
                if bucket is None:
                    try:
                        dt = datetime.strptime(ts_str, _EVENT_NAME_FORMAT).replace(tzinfo=UTC)
                    except ValueError:
                        continue
                    bucket = (
                        dt.timestamp(),
                        dict.fromkeys(CAMERA_KEYS),
                        dict.fromkeys(CAMERA_KEYS, False),
                    )
                    buckets[ts_str] = bucket
                _ts, cameras_dict, encrypted_dict = bucket
                cameras_dict[camera] = entry.name
                if not is_valid_mp4(Path(entry.path)):
                    encrypted_dict[camera] = True
    except OSError:
        return ()
    clips: list[Clip] = []
    for ts_str, (ts, cameras_dict, encrypted_dict) in buckets.items():
        clips.append(
            Clip(
                timestamp_str=ts_str,
                timestamp=ts,
                camera_videos=CameraVideos(**cameras_dict),
                encrypted_videos=EncryptedFlags(**encrypted_dict),
            )
        )
    clips.sort(key=lambda c: c.timestamp)
    return tuple(clips)


def _split_clip_filename(name: str) -> tuple[str | None, str | None]:
    """Split ``YYYY-MM-DD_HH-MM-SS-camera.mp4`` into ``(timestamp, camera)``."""
    parts = name.rsplit("-", 1)
    if len(parts) != _SPLIT_PARTS_EXPECTED:
        return None, None
    ts_str = parts[0]
    camera_with_ext = parts[1]
    if "." not in camera_with_ext:
        return None, None
    camera = camera_with_ext.rsplit(".", 1)[0].lower()
    return ts_str, camera


def _bucket_flat_sessions(
    folder_path: Path,
) -> tuple[dict[str, float], dict[str, list[ClipFile]]]:
    timestamps: dict[str, float] = {}
    files: dict[str, list[ClipFile]] = {}
    try:
        with os.scandir(folder_path) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.lower().endswith(_VIDEO_EXTENSIONS):
                    continue
                ts_str, _camera = _split_clip_filename(entry.name)
                if ts_str is None:
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                clip = ClipFile(
                    name=entry.name,
                    path=entry.path,
                    size_bytes=st.st_size,
                    mtime=st.st_mtime,
                )
                files.setdefault(ts_str, []).append(clip)
                prev = timestamps.get(ts_str, 0.0)
                if st.st_mtime > prev:
                    timestamps[ts_str] = st.st_mtime
    except OSError as exc:
        logger.debug("_bucket_flat_sessions: %s: %s", folder_path, exc)
        return {}, {}
    return timestamps, files


def _event_timestamp(event_name: str, fallback_mtime: float) -> float:
    try:
        dt = datetime.strptime(event_name, _EVENT_NAME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return fallback_mtime if fallback_mtime > 0 else 0.0
    return dt.timestamp()


def _pick_starting_clip(clips: tuple[Clip, ...], event_timestamp: float) -> int:
    if not clips:
        return 0
    starting = 0
    for i, clip in enumerate(clips):
        if clip.timestamp <= event_timestamp:
            starting = i
        else:
            break
    return starting
