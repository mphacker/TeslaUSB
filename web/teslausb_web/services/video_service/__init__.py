"""Public facade for the videos blueprint.

Architectural rule: this package MUST NOT import ``flask``. The
:class:`VideoService` is constructed by ``app.py`` (Layer 4) and
passed plain ``WebConfig`` + plain :class:`pathlib.Path` roots.
Everything below this module operates on the filesystem only —
which is what makes the per-file unit tests cheap.

The facade exists so callers (blueprint, tests) have one stable
import surface even if the internal split changes; ``_filesystem``,
``_paths``, ``_range``, ``_zip``, and ``_models`` are package-
private (leading underscore) per the charter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from teslausb_web.services.privileged_delete import (
    PrivilegedClipDeleter,
    PrivilegedDeleteError,
)
from teslausb_web.services.video_service._filesystem import (
    count_videos,
    event_playback_target,
    get_session_files,
    group_flat_sessions,
    is_valid_mp4,
    list_event_folders,
    parse_event_full,
    parse_event_lightweight,
)
from teslausb_web.services.video_service._models import (
    CAMERA_KEYS,
    CameraVideos,
    Clip,
    ClipFile,
    DeleteOutcome,
    EncryptedFlags,
    EventDetails,
    EventFolder,
    EventSummary,
    RangeRequest,
    SessionGroup,
)
from teslausb_web.services.video_service._paths import (
    ClipPermissionError,
    DeletionError,
    PathSecurityError,
    ResolvedClip,
    assert_inside,
    resolve_clip_path,
    safe_delete_clip,
)
from teslausb_web.services.video_service._range import RangeParseError, parse_range
from teslausb_web.services.video_service._zip import build_event_zip

if TYPE_CHECKING:
    from collections.abc import Iterator

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_TESLACAM_DIRNAME = "TeslaCam"
_RECENT_CLIPS_NAME = "RecentClips"
_DEFAULT_PER_PAGE = 12
_ZIP_CACHE_SUBDIR = ".cache/zip_temp"
_STREAM_CHUNK_SIZE = 256 * 1024


@dataclass(frozen=True, slots=True)
class VideoService:
    """Read-only operations over the on-disk TeslaCam tree.

    All paths are absolute. The service holds a single root
    ``teslacam_root`` (e.g. ``backing_root/TeslaCam``) which may be
    missing on disk — every method handles that case.
    """

    teslacam_root: Path

    # Optional fallback used ONLY when a direct delete raises
    # ``ClipPermissionError`` (the web user ``pi`` cannot rmtree a
    # teslausb-owned 0755 event dir). ``None`` disables the fallback —
    # the permission error then propagates as a ``DeletionError`` exactly
    # as before. Wired by :func:`make_video_service` from config.
    clip_deleter: PrivilegedClipDeleter | None = None

    # ------------------------------------------------------------------
    # Folder + event listings.

    def list_folders(self) -> list[EventFolder]:
        """List top-level folders the UI can browse."""
        folders: list[EventFolder] = []
        if self.teslacam_root.is_dir():
            try:
                for entry in self.teslacam_root.iterdir():
                    if not entry.is_dir():
                        continue
                    structure = "flat" if entry.name == _RECENT_CLIPS_NAME else "events"
                    folders.append(
                        EventFolder(
                            name=entry.name,
                            path=str(entry),
                            structure=structure,
                        )
                    )
            except OSError as exc:
                logger.warning("list_folders: %s: %s", self.teslacam_root, exc)
        folders.sort(key=lambda f: f.name)
        return folders

    def get_events(
        self,
        folder: str,
        *,
        page: int = 1,
        per_page: int = _DEFAULT_PER_PAGE,
    ) -> tuple[list[EventSummary], int]:
        """Paginated event listing for ``folder``."""
        folder_path = self._folder_path(folder)
        if folder_path is None or not folder_path.is_dir():
            return [], 0
        raw = list_event_folders(folder_path)
        total = len(raw)
        start = (page - 1) * per_page
        end = start + per_page
        out: list[EventSummary] = []
        for name, path, _mtime in raw[start:end]:
            parsed = parse_event_lightweight(path, name)
            if parsed is not None:
                out.append(parsed)
        return out, total

    def get_event_details(self, folder: str, event_name: str) -> EventDetails | None:
        """Full detail load for one event."""
        folder_path = self._folder_path(folder)
        if folder_path is None or not folder_path.is_dir():
            return None
        event_path = folder_path / Path(event_name).name
        if not event_path.is_dir():
            return None
        return parse_event_full(event_path, event_path.name)

    def group_videos_by_session(
        self,
        folder: str,
        *,
        page: int = 1,
        per_page: int = _DEFAULT_PER_PAGE,
    ) -> tuple[list[SessionGroup], int]:
        """Flat-folder grouping for RecentClips."""
        folder_path = self._folder_path(folder)
        if folder_path is None or not folder_path.is_dir():
            return [], 0
        return group_flat_sessions(folder_path, page=page, per_page=per_page)

    def count_videos_in_folder(self, folder: str) -> int:
        """Total ``*.mp4`` file count for a folder (one level deep)."""
        folder_path = self._folder_path(folder)
        if folder_path is None or not folder_path.is_dir():
            return 0
        return count_videos(folder_path)

    def get_folder_structure(self, folder: str) -> str:
        """Return ``"flat"`` or ``"events"`` for ``folder``."""
        if folder == _RECENT_CLIPS_NAME:
            return "flat"
        return "events"

    # ------------------------------------------------------------------
    # Path resolution + streaming.

    def is_valid_mp4(self, path: Path) -> bool:
        return is_valid_mp4(path)

    def resolve_clip_path(self, filepath: str) -> ResolvedClip:
        """Resolve a URL-supplied clip path under the allow-list."""
        return resolve_clip_path(filepath, self._allowed_roots())

    def stream_iter(
        self, path: Path, start: int, end: int, *, chunk_size: int = _STREAM_CHUNK_SIZE
    ) -> Iterator[bytes]:
        """Yield ``[start, end]`` (inclusive) of ``path`` in chunks."""
        with path.open("rb") as fp:
            fp.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fp.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    def parse_range(self, header: str | None, file_size: int) -> RangeRequest | None:
        return parse_range(header, file_size)

    # ------------------------------------------------------------------
    # Bulk operations.

    def download_event_zip(self, folder: str, event_name: str) -> tuple[Path, str]:
        """Write a zip of an event's videos to disk; return ``(path, filename)``.

        Caller (the blueprint) is responsible for unlinking the temp
        file after the response is sent.
        """
        folder_path = self._folder_path(folder)
        if folder_path is None or not folder_path.is_dir():
            raise FileNotFoundError(folder)
        structure = self.get_folder_structure(folder)
        sanitized_event = Path(event_name).name
        files = self._collect_event_files(folder_path, sanitized_event, structure)
        if not files:
            raise FileNotFoundError(f"no videos for {folder}/{event_name}")
        cache_dir = self.teslacam_root.parent / _ZIP_CACHE_SUBDIR
        zip_path = build_event_zip(tuple(files), cache_dir)
        return zip_path, f"{sanitized_event}.zip"

    def iter_zip_file(self, path: Path) -> Iterator[bytes]:
        """Yield ``path`` in chunks — used by tests; the blueprint hands
        the temp file straight to :func:`flask.send_file`.
        """
        with path.open("rb") as fp:
            while True:
                chunk = fp.read(_STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    def safe_delete_clip(self, folder: str, event_name: str) -> DeleteOutcome:
        """Delete an event or session's clips under the allow-list.

        For ``events``-structure folders, deletes the entire event
        subdirectory. For ``flat``-structure folders, deletes every
        clip matching the session-id prefix.
        """
        folder_path = self._folder_path(folder)
        if folder_path is None or not folder_path.is_dir():
            raise FileNotFoundError(folder)
        structure = self.get_folder_structure(folder)
        sanitized_event = Path(event_name).name
        deleted: list[str] = []
        deleted_paths: list[Path] = []
        errors = 0
        if structure == "flat":
            for clip in get_session_files(folder_path, sanitized_event):
                clip_path = Path(clip.path)
                try:
                    self._delete_one(clip_path)
                    deleted.append(clip.name)
                    deleted_paths.append(clip_path)
                except (DeletionError, PathSecurityError) as exc:
                    logger.warning("safe_delete_clip: %s: %s", clip.path, exc)
                    errors += 1
        else:
            event_path = folder_path / sanitized_event
            if not event_path.is_dir():
                raise FileNotFoundError(f"event not found: {folder}/{event_name}")
            # Snapshot filenames before delete so the response can list them.
            try:
                for entry in event_path.iterdir():
                    if entry.is_file():
                        deleted.append(entry.name)
                        deleted_paths.append(entry)
            except OSError as exc:
                logger.warning("safe_delete_clip: scan failed %s: %s", event_path, exc)
            self._delete_one(event_path)
        return DeleteOutcome(
            deleted_files=tuple(deleted),
            deleted_count=len(deleted),
            error_count=errors,
            deleted_paths=tuple(deleted_paths),
        )

    # ------------------------------------------------------------------
    # Private helpers.

    def _allowed_roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        if self.teslacam_root.exists():
            roots.append(self.teslacam_root)
        return tuple(roots)

    def _delete_one(self, path: Path) -> None:
        """Delete one clip path, falling back to the privileged helper.

        Attempts a direct delete first (the common case on a correctly
        permissioned tree). Only when that raises
        :class:`ClipPermissionError` — the web user ``pi`` cannot remove a
        teslausb-owned 0755 event dir — does it retry via the root-owned
        helper. With no ``clip_deleter`` configured the permission error
        propagates unchanged (still a ``DeletionError`` subclass).
        """
        try:
            safe_delete_clip(path, self._allowed_roots())
        except ClipPermissionError:
            if self.clip_deleter is None:
                raise
            logger.info("direct delete denied, retrying via privileged helper: %s", path)
            try:
                self.clip_deleter.delete(path)
            except PrivilegedDeleteError as exc:
                raise DeletionError(f"privileged delete failed: {path}: {exc}") from exc

    def _folder_path(self, folder: str) -> Path | None:
        """Map a logical folder name to its on-disk path."""
        sanitized = Path(folder).name
        if not sanitized:
            return None
        return self.teslacam_root / sanitized

    def _collect_event_files(
        self, folder_path: Path, event_name: str, structure: str
    ) -> list[tuple[Path, str]]:
        out: list[tuple[Path, str]] = []
        if structure == "flat":
            out.extend(
                (Path(clip.path), clip.name) for clip in get_session_files(folder_path, event_name)
            )
            return out
        event_path = folder_path / event_name
        details = parse_event_full(event_path, event_name)
        if details is None:
            return out
        for filename in details.camera_videos.to_dict().values():
            if filename:
                clip_path = event_path / filename
                if clip_path.is_file():
                    out.append((clip_path, filename))
        return out


def make_video_service(cfg: WebConfig) -> VideoService:
    """Construct the singleton for the gunicorn worker.

    Mirrors the ``make_*`` factory pattern used by every other
    service in this package — see ``services/cleanup/__init__.py``
    and the call site in ``app.py``.
    """
    return VideoService(
        teslacam_root=cfg.paths.backing_root / _TESLACAM_DIRNAME,
        clip_deleter=PrivilegedClipDeleter(
            script=cfg.paths.delete_clip_script,
            sudo_prefix=cfg.samba.sudo_prefix,
        ),
    )


__all__ = (
    "CAMERA_KEYS",
    "CameraVideos",
    "Clip",
    "ClipFile",
    "ClipPermissionError",
    "DeleteOutcome",
    "DeletionError",
    "EncryptedFlags",
    "EventDetails",
    "EventFolder",
    "EventSummary",
    "PathSecurityError",
    "PrivilegedClipDeleter",
    "PrivilegedDeleteError",
    "RangeParseError",
    "RangeRequest",
    "ResolvedClip",
    "SessionGroup",
    "VideoService",
    "assert_inside",
    "event_playback_target",
    "make_video_service",
)
