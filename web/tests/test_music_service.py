"""Tests for ``teslausb_web.services.music_service``."""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from teslausb_web.config import MusicSection, PathsSection, WebConfig
from teslausb_web.services.music_service import (
    MusicDirectory,
    MusicError,
    MusicFile,
    MusicFileError,
    MusicListing,
    MusicService,
    make_music_service,
)
from werkzeug.datastructures import FileStorage


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "backing"


@pytest.fixture
def music_folder(storage_root: Path) -> Path:
    return storage_root / "Music"


@pytest.fixture
def service(storage_root: Path, music_folder: Path) -> MusicService:
    return MusicService(
        storage_root=storage_root,
        music_folder=music_folder,
        max_file_size=16,
        chunk_size=8,
        free_space_reserve=4,
        stale_chunk_age=1,
        allowed_extensions=(".mp3", ".flac", ".wav", ".aac", ".m4a"),
    )


def _upload(name: str, payload: bytes) -> FileStorage:
    return FileStorage(stream=BytesIO(payload), filename=name)


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_list_files_returns_empty_listing_when_root_missing(service: MusicService) -> None:
    listing = service.list_files()

    assert isinstance(listing, MusicListing)
    assert listing.directories == ()
    assert listing.files == ()
    assert listing.relative_path == ""
    assert listing.total_bytes >= listing.used_bytes


def test_list_files_returns_sorted_directories_and_supported_files(
    service: MusicService,
    music_folder: Path,
) -> None:
    (music_folder / "Bravo").mkdir(parents=True)
    (music_folder / "alpha").mkdir()
    (music_folder / ".uploads").mkdir()
    _write(music_folder / "SongB.mp3", b"bb")
    _write(music_folder / "songA.wav", b"aa")
    _write(music_folder / "skip.txt", b"x")

    listing = service.list_files()

    assert [entry.name for entry in listing.directories] == ["alpha", "Bravo"]
    assert [entry.name for entry in listing.files] == ["songA.wav", "SongB.mp3"]
    assert all(isinstance(entry, MusicDirectory) for entry in listing.directories)
    assert all(isinstance(entry, MusicFile) for entry in listing.files)
    assert listing.files[0].modified_at.tzinfo is not None


def test_list_files_lists_nested_relative_path(service: MusicService, music_folder: Path) -> None:
    nested = music_folder / "Albums" / "Ünicode"
    nested.mkdir(parents=True)
    _write(nested / "track.m4a", b"payload")

    listing = service.list_files("Albums/Ünicode")

    assert listing.relative_path == "Albums/Ünicode"
    assert [entry.path for entry in listing.files] == ["Albums/Ünicode/track.m4a"]


def test_list_files_rejects_missing_nested_folder(service: MusicService) -> None:
    with pytest.raises(MusicError, match="Folder not found"):
        service.list_files("missing")


def test_resolve_file_path_returns_existing_music_file(
    service: MusicService, music_folder: Path
) -> None:
    expected = _write(music_folder / "folder" / "track.mp3", b"music")

    resolved = service.resolve_file_path("folder/track.mp3")

    assert resolved == expected


def test_resolve_file_path_rejects_missing_or_invalid_file(
    service: MusicService, music_folder: Path
) -> None:
    _write(music_folder / "folder" / "track.txt", b"music")

    with pytest.raises(MusicError, match="Unsupported file type"):
        service.resolve_file_path("folder/track.txt")
    with pytest.raises(MusicError, match="File not found"):
        service.resolve_file_path("folder/missing.mp3")


def test_upload_files_saves_multiple_music_files(service: MusicService, music_folder: Path) -> None:
    result = service.upload_files([_upload("one.mp3", b"1234"), _upload("two.wav", b"5678")])

    assert result.success is True
    assert result.file_count == 2
    assert (music_folder / "one.mp3").read_bytes() == b"1234"
    assert (music_folder / "two.wav").read_bytes() == b"5678"


def test_upload_files_returns_false_when_no_candidates(service: MusicService) -> None:
    assert service.upload_files([]).success is False
    assert service.upload_files([_upload("", b"x")]).message == "No files selected"


def test_upload_files_collects_partial_failures(service: MusicService, music_folder: Path) -> None:
    result = service.upload_files([_upload("ok.mp3", b"ok"), _upload("bad.txt", b"bad")])

    assert result.success is True
    assert result.file_count == 1
    assert "Errors:" in result.message
    assert (music_folder / "ok.mp3").exists()


def test_upload_files_accepts_unicode_filename(service: MusicService, music_folder: Path) -> None:
    result = service.upload_files([_upload("Ünicode song.m4a", b"payload")])

    assert result.success is True
    assert (music_folder / "Ünicode song.m4a").read_bytes() == b"payload"


def test_upload_files_accepts_exact_boundary_size(
    service: MusicService, music_folder: Path
) -> None:
    result = service.upload_files([_upload("exact.flac", b"x" * 16)])

    assert result.success is True
    assert (music_folder / "exact.flac").stat().st_size == 16


def test_upload_files_rejects_oversize_file(service: MusicService, music_folder: Path) -> None:
    result = service.upload_files([_upload("big.mp3", b"x" * 17)])

    assert result.success is False
    assert result.file_count == 0
    assert "Limit is" in result.message
    assert not (music_folder / "big.mp3").exists()


@pytest.mark.parametrize("name", ["../../evil.mp3", "folder\\evil.mp3"])
def test_upload_files_rejects_path_traversal(service: MusicService, name: str) -> None:
    result = service.upload_files([_upload(name, b"payload")])

    assert result.success is False
    assert "Invalid filename" in result.message


def test_upload_files_rejects_when_free_space_is_too_low(service: MusicService) -> None:
    with patch.object(service, "_disk_usage", return_value=(100, 90, 10)):
        result = service.upload_files([_upload("track.mp3", b"payload")])

    assert result.success is False
    assert result.message == "Not enough free space on Music drive"


def test_upload_files_raises_typed_error_on_filesystem_failure(service: MusicService) -> None:
    with (
        patch("teslausb_web.services.music_service.os.replace", side_effect=OSError("boom")),
        pytest.raises(MusicFileError, match="Failed to write"),
    ):
        service.upload_files([_upload("track.mp3", b"payload")])


def test_save_file_returns_single_file_message(service: MusicService) -> None:
    result = service.save_file(_upload("solo.aac", b"payload"))

    assert result.success is True
    assert result.file_count == 1
    assert result.message == "Uploaded solo.aac"


def test_handle_chunk_stores_intermediate_then_finalizes(
    service: MusicService, music_folder: Path
) -> None:
    first = service.handle_chunk("a" * 32, "chunk.mp3", 0, 2, 8, BytesIO(b"1234"))
    final = service.handle_chunk("a" * 32, "chunk.mp3", 1, 2, 8, BytesIO(b"5678"))

    assert first.success is True
    assert first.is_finalized is False
    assert first.message == "Chunk stored"
    assert final.success is True
    assert final.is_finalized is True
    assert (music_folder / "chunk.mp3").read_bytes() == b"12345678"


def test_handle_chunk_rejects_invalid_upload_id(service: MusicService) -> None:
    with pytest.raises(MusicError, match="Invalid upload ID"):
        service.handle_chunk("nope", "chunk.mp3", 0, 1, 4, BytesIO(b"data"))


def test_handle_chunk_rejects_size_mismatch(service: MusicService) -> None:
    with pytest.raises(MusicError, match="Size mismatch"):
        service.handle_chunk("b" * 32, "chunk.mp3", 0, 1, 5, BytesIO(b"data"))


def test_handle_chunk_purges_stale_part_files(service: MusicService, music_folder: Path) -> None:
    uploads_dir = music_folder / "nested" / ".uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    stale = _write(uploads_dir / "stale.part", b"old")
    old_time = stale.stat().st_mtime - 10
    os.utime(stale, (old_time, old_time))

    result = service.handle_chunk("c" * 32, "fresh.mp3", 0, 1, 4, BytesIO(b"data"), "nested")

    assert result.is_finalized is True
    assert not stale.exists()
    assert (music_folder / "nested" / "fresh.mp3").read_bytes() == b"data"


def test_delete_file_removes_nested_music_file(service: MusicService, music_folder: Path) -> None:
    _write(music_folder / "folder" / "delete.mp3", b"payload")

    result = service.delete_file("folder/delete.mp3")

    assert result.success is True
    assert result.deleted_count == 1
    assert not (music_folder / "folder" / "delete.mp3").exists()


def test_delete_file_returns_false_for_missing_file(service: MusicService) -> None:
    result = service.delete_file("missing.mp3")

    assert result.success is False
    assert result.message == "File not found"


def test_bulk_delete_deduplicates_and_reports_missing(
    service: MusicService, music_folder: Path
) -> None:
    _write(music_folder / "one.mp3", b"1")
    _write(music_folder / "two.wav", b"2")

    result = service.bulk_delete(["one.mp3", "one.mp3", "missing.mp3", "two.wav"])

    assert result.success is True
    assert result.deleted_count == 2
    assert "Errors:" in result.message
    assert not (music_folder / "one.mp3").exists()
    assert not (music_folder / "two.wav").exists()


def test_create_directory_creates_unicode_child_folder(
    service: MusicService, music_folder: Path
) -> None:
    music_folder.mkdir(parents=True, exist_ok=True)

    result = service.create_directory("", "  Ünicode   Folder  ")

    assert result.success is True
    assert (music_folder / "Ünicode Folder").is_dir()


def test_create_directory_rejects_duplicate(service: MusicService, music_folder: Path) -> None:
    (music_folder / "Existing").mkdir(parents=True)

    result = service.create_directory("", "Existing")

    assert result.success is False
    assert result.message == "Folder already exists"


def test_delete_directory_removes_tree(service: MusicService, music_folder: Path) -> None:
    _write(music_folder / "Albums" / "track.mp3", b"payload")

    result = service.delete_directory("Albums")

    assert result.success is True
    assert not (music_folder / "Albums").exists()


def test_delete_directory_rejects_root(service: MusicService) -> None:
    result = service.delete_directory("")

    assert result.success is False
    assert result.message == "Cannot delete root folder"


def test_move_file_moves_and_renames_music_file(service: MusicService, music_folder: Path) -> None:
    _write(music_folder / "source" / "track.mp3", b"payload")

    result = service.move_file("source/track.mp3", "dest", "renamed.mp3")

    assert result.success is True
    assert result.destination_path == "dest/renamed.mp3"
    assert not (music_folder / "source" / "track.mp3").exists()
    assert (music_folder / "dest" / "renamed.mp3").read_bytes() == b"payload"


def test_move_file_returns_false_for_missing_source(service: MusicService) -> None:
    result = service.move_file("missing.mp3", "dest")

    assert result.success is False
    assert result.message == "Source file not found"


def test_generate_upload_id_returns_hex_string(service: MusicService) -> None:
    upload_id = service.generate_upload_id()

    assert len(upload_id) == 32
    assert int(upload_id, 16) >= 0


def test_factory_uses_configured_paths() -> None:
    cfg = WebConfig(
        paths=PathsSection(backing_root=Path("/srv/teslausb"), state_dir=Path("/var/lib/teslausb")),
        music=MusicSection(
            folder="Audio",
            max_file_size=64,
            chunk_size=8,
            free_space_reserve=12,
            stale_chunk_age=30,
            allowed_extensions=(".mp3", ".wav"),
        ),
    )

    built = make_music_service(cfg)

    assert built._storage_root == Path("/srv/teslausb")
    assert built._music_folder == Path("/srv/teslausb") / "Audio"
    assert built._max_file_size == 64
    assert built._chunk_size == 8
    assert built._free_space_reserve == 12
    assert built._stale_chunk_age == 30
    assert built._allowed_extensions == (".mp3", ".wav")
