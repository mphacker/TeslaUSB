"""Frozen dataclasses returned by :class:`VideoService`.

All public-facing types live here so the rest of the package can
import them without dragging in business logic. Every dataclass is
``frozen=True, slots=True`` so blueprint callers cannot accidentally
mutate service-returned state (charter Pillar 3: no shortcut globals,
no shared-mutable side-channels).

There is no Flask import in this module — see the package
``__init__`` docstring for the architectural-layering rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# Tesla-camera filename suffixes we recognise. Keyed by canonical
# camera id; the strings are what appears between the trailing ``-``
# and ``.mp4`` in a TeslaCam clip name. Order matters for the
# longest-prefix match in :mod:`_filesystem` (``left_repeater`` must
# beat ``left``).
CAMERA_KEYS: Final[tuple[str, ...]] = (
    "front",
    "back",
    "left_repeater",
    "right_repeater",
    "left_pillar",
    "right_pillar",
)


@dataclass(frozen=True, slots=True)
class EventFolder:
    """A top-level folder under ``TeslaCam/`` (or the archive root).

    ``structure`` is one of ``"flat"`` (one mp4 per camera per
    timestamp, all in the folder root — RecentClips, ArchivedClips)
    or ``"events"`` (one subdirectory per event, each containing the
    six camera angles — SavedClips, SentryClips).
    """

    name: str
    path: str
    structure: str


@dataclass(frozen=True, slots=True)
class ClipFile:
    """A single mp4 belonging to a clip-set on disk."""

    name: str
    path: str
    size_bytes: int
    mtime: float


@dataclass(frozen=True, slots=True)
class CameraVideos:
    """Mapping of camera-id to filename for one timestamp."""

    front: str | None = None
    back: str | None = None
    left_repeater: str | None = None
    right_repeater: str | None = None
    left_pillar: str | None = None
    right_pillar: str | None = None
    event: str | None = None  # the synthesised "grid view" mp4

    def to_dict(self) -> dict[str, str | None]:
        """Render as the dict the template expects.

        v1 served raw dicts via ``jsonify``; the template / panel JS
        keys off these names. Returning a dict here means the
        blueprint serialiser doesn't need to know the field list.
        """
        return {
            "front": self.front,
            "back": self.back,
            "left_repeater": self.left_repeater,
            "right_repeater": self.right_repeater,
            "left_pillar": self.left_pillar,
            "right_pillar": self.right_pillar,
            "event": self.event,
        }

    def any_present(self) -> bool:
        return any(self.to_dict().values())


@dataclass(frozen=True, slots=True)
class EncryptedFlags:
    """Per-camera flag set when an mp4 is missing its ``ftyp`` header.

    Tesla encrypts some camera angles in RecentClips until they're
    saved. We surface the flag so the UI can grey out the relevant
    tile rather than failing playback silently.
    """

    front: bool = False
    back: bool = False
    left_repeater: bool = False
    right_repeater: bool = False
    left_pillar: bool = False
    right_pillar: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "front": self.front,
            "back": self.back,
            "left_repeater": self.left_repeater,
            "right_repeater": self.right_repeater,
            "left_pillar": self.left_pillar,
            "right_pillar": self.right_pillar,
        }


@dataclass(frozen=True, slots=True)
class Clip:
    """One sub-clip inside an event (SavedClips has many; SentryClips
    typically one).
    """

    timestamp_str: str
    timestamp: float
    camera_videos: CameraVideos
    encrypted_videos: EncryptedFlags


@dataclass(frozen=True, slots=True)
class EventSummary:
    """Lightweight event record returned by :func:`get_events`.

    No clip-level encryption probe — that requires opening every mp4
    and would dominate the page-load cost for SentryClips with
    thousands of events.
    """

    name: str
    timestamp: float
    datetime_str: str
    size_mb: float
    camera_videos: CameraVideos
    city: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class EventDetails:
    """Full event record returned by :func:`get_event_details`."""

    name: str
    path: str
    timestamp: float
    datetime_str: str
    size_bytes: int
    size_mb: float
    camera_videos: CameraVideos
    encrypted_videos: EncryptedFlags
    metadata: dict[str, object] = field(default_factory=dict)
    city: str = ""
    reason: str = ""
    clips: tuple[Clip, ...] = ()
    starting_clip_index: int = 0


@dataclass(frozen=True, slots=True)
class SessionGroup:
    """A flat-folder (RecentClips/ArchivedClips) session group."""

    name: str
    timestamp: float
    datetime_str: str
    size_mb: float
    camera_videos: CameraVideos
    encrypted_videos: EncryptedFlags
    city: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RangeRequest:
    """Parsed HTTP Range header (single byte-range only)."""

    start: int
    end: int  # inclusive
    full_size: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    """Result of :func:`safe_delete_clip` / :func:`safe_delete_event`."""

    deleted_files: tuple[str, ...]
    deleted_count: int
    error_count: int
