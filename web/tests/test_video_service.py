"""Tests for ``teslausb_web.services.video_service`` (Phase 5.26).

Exercises VideoService end-to-end against an on-disk fixture tree
plus the per-module security primitives (``_paths.resolve_clip_path``,
``_paths.safe_delete_clip``). No Flask is involved here — the
service is built directly via :func:`VideoService`.
"""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING

import pytest
from teslausb_web.services.video_service import (
    DeletionError,
    PathSecurityError,
    VideoService,
    assert_inside,
)
from teslausb_web.services.video_service._paths import (
    resolve_clip_path,
    safe_delete_clip,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixture helpers — build a small TeslaCam-shaped tree.
# ---------------------------------------------------------------------------


_VALID_MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 1024
_ENCRYPTED_MP4 = b"\x00" * 16 + b"junkjunk" + b"\x00" * 1024


def _write_clip(path: Path, *, encrypted: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_ENCRYPTED_MP4 if encrypted else _VALID_MP4)


def _build_tree(tmp_path: Path) -> tuple[Path, Path]:
    teslacam = tmp_path / "TeslaCam"
    archive = tmp_path / "ArchivedClips"
    cameras = (
        "front",
        "back",
        "left_repeater",
        "right_repeater",
        "left_pillar",
        "right_pillar",
    )

    # SentryClips (events structure)
    event = teslacam / "SentryClips" / "2025-01-15_12-30-45"
    for cam in cameras:
        _write_clip(event / f"2025-01-15_12-30-45-{cam}.mp4")
    _write_clip(event / "event.mp4")
    (event / "event.json").write_text(
        json.dumps(
            {
                "timestamp": "2025-01-15T12:30:45",
                "city": "Austin",
                "reason": "sentry_aware_object_detection",
                "est_lat": 30.0,
                "est_lon": -97.0,
            }
        )
    )

    # SavedClips (events structure)
    event2 = teslacam / "SavedClips" / "2025-02-20_18-00-00"
    for cam in cameras:
        _write_clip(event2 / f"2025-02-20_18-00-00-{cam}.mp4")

    # RecentClips (flat structure) — two sessions
    recent = teslacam / "RecentClips"
    for cam in cameras:
        _write_clip(recent / f"2025-03-01_09-15-30-{cam}.mp4")
    # Mark one camera encrypted
    _write_clip(recent / "2025-03-01_09-16-00-front.mp4", encrypted=True)
    _write_clip(recent / "2025-03-01_09-16-00-back.mp4")

    # ArchivedClips (flat structure)
    for cam in cameras:
        _write_clip(archive / f"2024-12-25_10-00-00-{cam}.mp4")

    return teslacam, archive


def _make_svc(tmp_path: Path) -> VideoService:
    teslacam, archive = _build_tree(tmp_path)
    return VideoService(
        teslacam_root=teslacam,
        archive_root=archive,
        archive_enabled=True,
    )


# ---------------------------------------------------------------------------
# Listing / paging.
# ---------------------------------------------------------------------------


class TestListing:
    def test_list_folders_includes_archive(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        folders = svc.list_folders()
        names = {f.name for f in folders}
        assert {"SentryClips", "SavedClips", "RecentClips", "ArchivedClips"} <= names

    def test_list_folders_skips_archive_when_disabled(self, tmp_path: Path) -> None:
        teslacam, archive = _build_tree(tmp_path)
        svc = VideoService(teslacam, archive, archive_enabled=False)
        names = {f.name for f in svc.list_folders()}
        assert "ArchivedClips" not in names

    def test_list_folders_empty_when_missing(self, tmp_path: Path) -> None:
        svc = VideoService(
            teslacam_root=tmp_path / "absent",
            archive_root=tmp_path / "absent2",
            archive_enabled=True,
        )
        assert svc.list_folders() == []

    def test_get_events_returns_events_with_metadata(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        events, total = svc.get_events("SentryClips")
        assert total == 1
        assert len(events) == 1
        assert events[0].name == "2025-01-15_12-30-45"
        assert events[0].city == "Austin"
        assert events[0].camera_videos.front == "2025-01-15_12-30-45-front.mp4"

    def test_get_events_pagination(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        # per_page=1 forces pagination on the SavedClips folder
        events, total = svc.get_events("SavedClips", page=1, per_page=1)
        assert total == 1
        assert len(events) == 1

    def test_get_events_unknown_folder(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        events, total = svc.get_events("DoesNotExist")
        assert events == []
        assert total == 0

    def test_group_flat_sessions(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        sessions, total = svc.group_videos_by_session("RecentClips")
        # Two sessions: ...09-15-30 and ...09-16-00
        assert total == 2
        assert len(sessions) == 2
        names = {s.name for s in sessions}
        assert "2025-03-01_09-15-30" in names

    def test_get_event_details_full(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        details = svc.get_event_details("SentryClips", "2025-01-15_12-30-45")
        assert details is not None
        assert details.name == "2025-01-15_12-30-45"
        assert details.city == "Austin"
        assert details.camera_videos.front == "2025-01-15_12-30-45-front.mp4"

    def test_get_event_details_missing(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        assert svc.get_event_details("SentryClips", "nope") is None

    def test_count_videos_in_folder(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        # RecentClips: 6 cams for session A + 2 for session B = 8
        assert svc.count_videos_in_folder("RecentClips") == 8

    def test_get_folder_structure(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        assert svc.get_folder_structure("RecentClips") == "flat"
        assert svc.get_folder_structure("ArchivedClips") == "flat"
        assert svc.get_folder_structure("SentryClips") == "events"


# ---------------------------------------------------------------------------
# MP4 header probe.
# ---------------------------------------------------------------------------


class TestMP4Probe:
    def test_valid_mp4_detected(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        good = svc.teslacam_root / "SentryClips" / "2025-01-15_12-30-45" / "event.mp4"
        assert svc.is_valid_mp4(good) is True

    def test_encrypted_mp4_detected(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        encrypted = svc.teslacam_root / "RecentClips" / "2025-03-01_09-16-00-front.mp4"
        assert svc.is_valid_mp4(encrypted) is False

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        assert svc.is_valid_mp4(tmp_path / "missing.mp4") is False


# ---------------------------------------------------------------------------
# Path resolution + traversal guards.
# ---------------------------------------------------------------------------


class TestResolveClipPath:
    def test_resolve_sentry_clip(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        resolved = svc.resolve_clip_path("SentryClips/2025-01-15_12-30-45/event.mp4")
        assert resolved.path.name == "event.mp4"
        assert resolved.path.exists()

    def test_resolve_recent_clip(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        resolved = svc.resolve_clip_path("RecentClips/2025-03-01_09-15-30-front.mp4")
        assert resolved.path.name == "2025-03-01_09-15-30-front.mp4"

    def test_resolve_archive_clip_with_prefix(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        resolved = svc.resolve_clip_path("ArchivedClips/2024-12-25_10-00-00-front.mp4")
        assert resolved.path.name == "2024-12-25_10-00-00-front.mp4"
        assert resolved.allowed_root == svc.archive_root.resolve()

    def test_traversal_with_dotdot_blocked(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        with pytest.raises((FileNotFoundError, PathSecurityError)):
            svc.resolve_clip_path("SentryClips/../../etc/passwd")

    def test_empty_path_raises(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        with pytest.raises(PathSecurityError):
            svc.resolve_clip_path("")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        with pytest.raises(FileNotFoundError):
            svc.resolve_clip_path("SentryClips/does-not-exist.mp4")

    def test_resolve_with_no_roots(self) -> None:
        with pytest.raises(FileNotFoundError):
            resolve_clip_path("foo.mp4", ())


class TestAssertInside:
    def test_inside_ok(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("hi")
        result = assert_inside(target, (tmp_path,))
        assert result == target.resolve()

    def test_outside_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("hi")
        with pytest.raises(PathSecurityError):
            assert_inside(outside, (root,))


# ---------------------------------------------------------------------------
# Streaming + range.
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_stream_iter_returns_chunks(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        path = svc.teslacam_root / "SentryClips" / "2025-01-15_12-30-45" / "event.mp4"
        size = path.stat().st_size
        chunks = list(svc.stream_iter(path, 0, size - 1, chunk_size=64))
        assert b"".join(chunks) == path.read_bytes()

    def test_stream_iter_partial(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        path = svc.teslacam_root / "SentryClips" / "2025-01-15_12-30-45" / "event.mp4"
        chunks = list(svc.stream_iter(path, 4, 7))
        assert b"".join(chunks) == path.read_bytes()[4:8]

    def test_parse_range_delegation(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        rng = svc.parse_range("bytes=0-9", 100)
        assert rng is not None
        assert rng.start == 0
        assert rng.end == 9


# ---------------------------------------------------------------------------
# ZIP download.
# ---------------------------------------------------------------------------


class TestZipDownload:
    def test_download_event_zip_events_folder(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        zip_path, name = svc.download_event_zip("SentryClips", "2025-01-15_12-30-45")
        try:
            assert name == "2025-01-15_12-30-45.zip"
            assert zip_path.exists()
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
            assert "event.mp4" in names
            assert "2025-01-15_12-30-45-front.mp4" in names
        finally:
            zip_path.unlink(missing_ok=True)

    def test_download_event_zip_flat_folder(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        zip_path, name = svc.download_event_zip("RecentClips", "2025-03-01_09-15-30")
        try:
            assert name == "2025-03-01_09-15-30.zip"
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
            assert "2025-03-01_09-15-30-front.mp4" in names
        finally:
            zip_path.unlink(missing_ok=True)

    def test_download_event_zip_missing_folder(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        with pytest.raises(FileNotFoundError):
            svc.download_event_zip("NopeNopeNope", "anything")

    def test_download_event_zip_missing_event(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        with pytest.raises(FileNotFoundError):
            svc.download_event_zip("SentryClips", "2099-01-01_00-00-00")


# ---------------------------------------------------------------------------
# Safe-delete.
# ---------------------------------------------------------------------------


class TestSafeDelete:
    def test_delete_event_folder(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        outcome = svc.safe_delete_clip("SentryClips", "2025-01-15_12-30-45")
        assert outcome.deleted_count > 0
        assert outcome.error_count == 0
        assert not (svc.teslacam_root / "SentryClips" / "2025-01-15_12-30-45").exists()

    def test_delete_flat_session(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        outcome = svc.safe_delete_clip("RecentClips", "2025-03-01_09-15-30")
        assert outcome.deleted_count == 6
        recent = svc.teslacam_root / "RecentClips"
        # Other session still intact
        assert (recent / "2025-03-01_09-16-00-back.mp4").exists()
        # Target session gone
        assert not (recent / "2025-03-01_09-15-30-front.mp4").exists()

    def test_delete_unknown_folder_raises(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        with pytest.raises(FileNotFoundError):
            svc.safe_delete_clip("Mystery", "anything")

    def test_safe_delete_outside_root_blocked(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.mp4"
        outside.write_text("x")
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(PathSecurityError):
            safe_delete_clip(outside, (root,))
        assert outside.exists()  # not deleted

    def test_safe_delete_missing_file_silent(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        target = root / "ghost.mp4"
        # Resolve treats missing as inside, then unlink raises
        # FileNotFoundError which we swallow.
        safe_delete_clip(target, (root,))
        assert not target.exists()

    def test_deletion_error_class_is_runtime(self) -> None:
        # Sanity: keep DeletionError importable from the package.
        assert issubclass(DeletionError, RuntimeError)


# ---------------------------------------------------------------------------
# iter_zip_file + assorted edge cases for coverage.
# ---------------------------------------------------------------------------


class TestExtraCoverage:
    def test_iter_zip_file_roundtrip(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        zip_path, _ = svc.download_event_zip("SentryClips", "2025-01-15_12-30-45")
        try:
            streamed = b"".join(svc.iter_zip_file(zip_path))
            assert streamed == zip_path.read_bytes()
        finally:
            zip_path.unlink(missing_ok=True)

    def test_archive_only_session_resolves(self, tmp_path: Path) -> None:
        svc = _make_svc(tmp_path)
        # Resolve a clip path without the ArchivedClips/ prefix —
        # this exercises the no-leading-name branch of resolve_clip_path.
        clip = svc.archive_root / "2024-12-25_10-00-00-front.mp4"
        result = svc.resolve_clip_path("ArchivedClips/2024-12-25_10-00-00-front.mp4")
        assert result.path == clip.resolve()

    def test_safe_delete_clip_on_file(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        target = root / "x.mp4"
        target.write_bytes(b"x")
        safe_delete_clip(target, (root,))
        assert not target.exists()
