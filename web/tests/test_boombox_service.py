"""Tests for ``teslausb_web.services.boombox_service``."""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from teslausb_web.config import BoomboxSection, MusicSection, PathsSection, WebConfig
from teslausb_web.services.boombox_service import (
    BoomboxError,
    BoomboxFile,
    BoomboxFileError,
    BoomboxListing,
    BoomboxService,
    make_boombox_service,
)
from werkzeug.datastructures import FileStorage

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def boombox_dir(tmp_path: Path) -> Path:
    path = tmp_path / "backing" / "Music" / "Boombox"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def scheduler() -> Mock:
    return Mock()


@pytest.fixture
def service(boombox_dir: Path, scheduler: Mock) -> BoomboxService:
    from teslausb_web.services.boombox_service import BoomboxConfig

    return BoomboxService(
        BoomboxConfig(
            base_dir=boombox_dir,
            max_file_bytes=8,
            max_files=5,
            allowed_extensions=(".mp3", ".wav"),
            schedule_cache_invalidation=scheduler,
        )
    )


def _upload(name: str, payload: bytes) -> FileStorage:
    return FileStorage(stream=BytesIO(payload), filename=name)


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_list_files_returns_empty_listing_when_folder_missing(tmp_path: Path) -> None:
    from teslausb_web.services.boombox_service import BoomboxConfig

    service = BoomboxService(
        BoomboxConfig(
            base_dir=tmp_path / "missing" / "Boombox",
            max_file_bytes=8,
            max_files=5,
            allowed_extensions=(".mp3", ".wav"),
        )
    )

    listing = service.list_files()

    assert listing == BoomboxListing(files=(), max_files=5)


def test_list_files_returns_sorted_supported_files(
    service: BoomboxService,
    boombox_dir: Path,
) -> None:
    _write(boombox_dir / "bravo.mp3", b"b")
    _write(boombox_dir / "Alpha.WAV", b"a")
    _write(boombox_dir / "skip.txt", b"x")
    (boombox_dir / "folder.wav").mkdir()

    listing = service.list_files()

    assert [entry.filename for entry in listing.files] == ["Alpha.WAV", "bravo.mp3"]
    assert all(isinstance(entry, BoomboxFile) for entry in listing.files)
    assert listing.max_files == 5
    assert listing.files[0].modified_at.tzinfo is not None


def test_list_files_skips_symlinks(service: BoomboxService, boombox_dir: Path) -> None:
    target = _write(boombox_dir / "real.mp3", b"x")
    try:
        (boombox_dir / "linked.mp3").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not available on this system")

    listing = service.list_files()

    assert [entry.filename for entry in listing.files] == ["real.mp3"]


def test_list_files_raises_typed_error_when_scanning_fails(service: BoomboxService) -> None:
    with (
        patch("pathlib.Path.iterdir", side_effect=OSError("boom")),
        pytest.raises(BoomboxFileError, match="Failed to list boombox files"),
    ):
        service.list_files()


def test_upload_file_saves_mp3(service: BoomboxService, boombox_dir: Path, scheduler: Mock) -> None:
    result = service.upload_file(_upload("horn.mp3", b"1234"))

    assert result.success is True
    assert result.message == "Uploaded horn.mp3"
    assert (boombox_dir / "horn.mp3").read_bytes() == b"1234"
    scheduler.assert_called_once_with()


def test_upload_file_saves_wav_case_insensitively(
    service: BoomboxService,
    boombox_dir: Path,
) -> None:
    result = service.upload_file(_upload("sound.WAV", b"1234"))

    assert result.success is True
    assert (boombox_dir / "sound.WAV").read_bytes() == b"1234"


def test_upload_file_returns_false_when_filename_missing(
    service: BoomboxService,
    scheduler: Mock,
) -> None:
    result = service.upload_file(_upload("", b"x"))

    assert result.success is False
    assert result.message == "No file selected"
    scheduler.assert_not_called()


def test_upload_file_rejects_blank_filename(service: BoomboxService, scheduler: Mock) -> None:
    result = service.upload_file(_upload("   ", b"x"))

    assert result.success is False
    assert result.message == "Filename is required"
    scheduler.assert_not_called()


@pytest.mark.parametrize("name", ["../../evil.mp3", "folder\\evil.mp3", ".", ".."])
def test_upload_file_rejects_path_traversal(
    service: BoomboxService,
    scheduler: Mock,
    name: str,
) -> None:
    result = service.upload_file(_upload(name, b"payload"))

    assert result.success is False
    assert "Invalid filename" in result.message
    scheduler.assert_not_called()


def test_upload_file_rejects_unsupported_extension(
    service: BoomboxService,
    scheduler: Mock,
) -> None:
    result = service.upload_file(_upload("note.txt", b"payload"))

    assert result.success is False
    assert result.message == "Only MP3 and WAV files are allowed"
    scheduler.assert_not_called()


def test_upload_file_accepts_exact_boundary_size(
    service: BoomboxService,
    boombox_dir: Path,
) -> None:
    result = service.upload_file(_upload("exact.wav", b"12345678"))

    assert result.success is True
    assert (boombox_dir / "exact.wav").stat().st_size == 8


def test_upload_file_rejects_oversize_file(service: BoomboxService, boombox_dir: Path) -> None:
    result = service.upload_file(_upload("big.mp3", b"123456789"))

    assert result.success is False
    assert result.file_count == 0
    assert "Limit is" in result.message
    assert not (boombox_dir / "big.mp3").exists()


def test_upload_file_overwrites_duplicate_filename(
    service: BoomboxService,
    boombox_dir: Path,
) -> None:
    first = service.upload_file(_upload("dup.mp3", b"old"))
    second = service.upload_file(_upload("dup.mp3", b"new"))

    assert first.success is True
    assert second.success is True
    assert (boombox_dir / "dup.mp3").read_bytes() == b"new"


def test_upload_file_allows_more_files_than_max_limit(
    service: BoomboxService,
    boombox_dir: Path,
) -> None:
    for index in range(6):
        result = service.upload_file(_upload(f"clip{index}.mp3", b"ok"))
        assert result.success is True

    listing = service.list_files()

    assert len(listing.files) == 6
    assert listing.max_files == 5
    assert [entry.filename for entry in listing.files][:5] == [
        "clip0.mp3",
        "clip1.mp3",
        "clip2.mp3",
        "clip3.mp3",
        "clip4.mp3",
    ]
    assert (boombox_dir / "clip5.mp3").exists()


def test_upload_file_tolerates_fsync_failure(service: BoomboxService, boombox_dir: Path) -> None:
    with patch("teslausb_web.services.boombox_service.os.fsync", side_effect=OSError("no fsync")):
        result = service.upload_file(_upload("nofsync.wav", b"1234"))

    assert result.success is True
    assert (boombox_dir / "nofsync.wav").read_bytes() == b"1234"


def test_upload_file_raises_typed_error_on_replace_failure(service: BoomboxService) -> None:
    with (
        patch("teslausb_web.services.boombox_service.os.replace", side_effect=OSError("boom")),
        pytest.raises(BoomboxFileError, match="Failed to write"),
    ):
        service.upload_file(_upload("broken.mp3", b"1234"))


def test_delete_file_removes_existing_file(
    service: BoomboxService,
    boombox_dir: Path,
    scheduler: Mock,
) -> None:
    _write(boombox_dir / "delete.wav", b"1234")

    result = service.delete_file("delete.wav")

    assert result.success is True
    assert result.deleted_count == 1
    assert not (boombox_dir / "delete.wav").exists()
    scheduler.assert_called_once_with()


def test_delete_file_returns_false_for_missing_file(
    service: BoomboxService,
    scheduler: Mock,
) -> None:
    result = service.delete_file("missing.mp3")

    assert result.success is False
    assert result.message == "File not found"
    scheduler.assert_not_called()


def test_delete_file_rejects_invalid_filename(service: BoomboxService) -> None:
    with pytest.raises(BoomboxError, match="Invalid filename"):
        service.delete_file("../../evil.mp3")


def test_delete_file_raises_typed_error_on_unlink_failure(
    service: BoomboxService,
    boombox_dir: Path,
) -> None:
    _write(boombox_dir / "broken.mp3", b"1234")

    with (
        patch("pathlib.Path.unlink", side_effect=OSError("boom")),
        pytest.raises(BoomboxFileError, match=r"Failed to delete boombox file broken\.mp3"),
    ):
        service.delete_file("broken.mp3")


def test_make_boombox_service_uses_music_folder_and_defaults(tmp_path: Path) -> None:
    scheduler = Mock()
    cfg = WebConfig(
        paths=PathsSection(backing_root=tmp_path / "backing"),
        music=MusicSection(folder="Audio"),
        boombox=BoomboxSection(base_dir="Boom", max_file_bytes=32, max_files=6),
    )

    service = make_boombox_service(cfg, schedule_cache_invalidation=scheduler)

    result = service.upload_file(_upload("tone.wav", b"1234"))

    assert result.success is True
    assert (tmp_path / "backing" / "Audio" / "Boom" / "tone.wav").read_bytes() == b"1234"
    scheduler.assert_called_once_with()


def test_make_boombox_service_uses_default_boombox_folder(tmp_path: Path) -> None:
    cfg = WebConfig(paths=PathsSection(backing_root=tmp_path / "backing"))

    service = make_boombox_service(cfg)
    result = service.upload_file(_upload("default.mp3", b"1234"))

    assert result.success is True
    assert (tmp_path / "backing" / "Music" / "Boombox" / "default.mp3").exists()


def test_upload_file_without_scheduler_succeeds(boombox_dir: Path) -> None:
    from teslausb_web.services.boombox_service import BoomboxConfig

    service = BoomboxService(
        BoomboxConfig(
            base_dir=boombox_dir,
            max_file_bytes=8,
            max_files=5,
            allowed_extensions=(".mp3", ".wav"),
        )
    )

    result = service.upload_file(_upload("plain.mp3", b"1234"))

    assert result.success is True
    assert (boombox_dir / "plain.mp3").read_bytes() == b"1234"


def test_delete_file_tolerates_directory_fsync_failure(
    service: BoomboxService,
    boombox_dir: Path,
) -> None:
    _write(boombox_dir / "nofsync.mp3", b"1234")

    with patch(
        "teslausb_web.services.boombox_service.os.open",
        side_effect=OSError("no dir fsync"),
    ):
        result = service.delete_file("nofsync.mp3")

    assert result.success is True
    assert not (boombox_dir / "nofsync.mp3").exists()
