from __future__ import annotations

import time
from pathlib import Path

from teslausb_web.services.cloud_archive.integrity import (
    BROKEN_VIDEO_IDLE_SECONDS,
    has_moov_atom,
    purge_broken_videos,
)


def _box(box_type: bytes, payload: bytes = b"") -> bytes:
    size = 8 + len(payload)
    return size.to_bytes(4, "big") + box_type + payload


def _write_good_mp4(path: Path) -> None:
    path.write_bytes(
        _box(b"ftyp", b"isom" + b"\x00" * 12)
        + _box(b"mdat", b"\x00" * 1024)
        + _box(b"moov", b"\x00" * 64)
    )


def _write_broken_mp4(path: Path) -> None:
    # ftyp + a partial mdat the writer never finished — no moov box.
    path.write_bytes(_box(b"ftyp", b"isom" + b"\x00" * 12) + _box(b"mdat", b"\x00" * 2048))


def _age_file(path: Path, seconds: float) -> None:
    target = time.time() - seconds
    import os

    os.utime(path, (target, target))


def test_has_moov_atom_finds_moov(tmp_path: Path) -> None:
    path = tmp_path / "good.mp4"
    _write_good_mp4(path)
    assert has_moov_atom(path) is True


def test_has_moov_atom_returns_false_without_moov(tmp_path: Path) -> None:
    path = tmp_path / "broken.mp4"
    _write_broken_mp4(path)
    assert has_moov_atom(path) is False


def test_has_moov_atom_handles_tiny_file(tmp_path: Path) -> None:
    path = tmp_path / "tiny.mp4"
    path.write_bytes(b"\x00\x00")
    assert has_moov_atom(path) is False


def test_has_moov_atom_handles_missing_file(tmp_path: Path) -> None:
    assert has_moov_atom(tmp_path / "nope.mp4") is False


def test_has_moov_atom_handles_malformed_size(tmp_path: Path) -> None:
    # A header that claims size=4 (smaller than the 8-byte header itself).
    path = tmp_path / "bogus.mp4"
    path.write_bytes(b"\x00\x00\x00\x04ftyp")
    assert has_moov_atom(path) is False


def test_purge_deletes_idle_broken_files(tmp_path: Path) -> None:
    recent = tmp_path / "RecentClips"
    recent.mkdir()
    good = recent / "2026-01-01_12-00-00-front.mp4"
    broken = recent / "2026-01-01_12-01-00-front.mp4"
    _write_good_mp4(good)
    _write_broken_mp4(broken)
    _age_file(good, BROKEN_VIDEO_IDLE_SECONDS + 60)
    _age_file(broken, BROKEN_VIDEO_IDLE_SECONDS + 60)

    report = purge_broken_videos(tmp_path, ("RecentClips",))

    assert good.exists()
    assert not broken.exists()
    assert report.scanned == 2
    assert report.broken_found == 1
    assert report.deleted == 1
    assert report.skipped_in_use == 0


def test_purge_skips_actively_written_files(tmp_path: Path) -> None:
    recent = tmp_path / "RecentClips"
    recent.mkdir()
    in_progress = recent / "2026-01-01_12-02-00-front.mp4"
    _write_broken_mp4(in_progress)
    # Brand-new mtime — Tesla is presumed to still be writing this file.

    report = purge_broken_videos(tmp_path, ("RecentClips",))

    assert in_progress.exists()
    assert report.broken_found == 1
    assert report.deleted == 0
    assert report.skipped_in_use == 1


def test_purge_walks_sentry_event_subdirs(tmp_path: Path) -> None:
    sentry = tmp_path / "SentryClips" / "2026-01-01_12-03-00"
    sentry.mkdir(parents=True)
    broken = sentry / "front.mp4"
    good = sentry / "back.mp4"
    _write_broken_mp4(broken)
    _write_good_mp4(good)
    _age_file(broken, BROKEN_VIDEO_IDLE_SECONDS + 60)
    _age_file(good, BROKEN_VIDEO_IDLE_SECONDS + 60)

    report = purge_broken_videos(tmp_path, ("SentryClips",))

    assert good.exists()
    assert not broken.exists()
    assert report.deleted == 1


def test_purge_no_op_when_folder_missing(tmp_path: Path) -> None:
    report = purge_broken_videos(tmp_path, ("RecentClips",))
    assert report.scanned == 0
    assert report.deleted == 0
