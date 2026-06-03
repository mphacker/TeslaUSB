"""Tests for ``teslausb_web.services.light_show_service``."""

from __future__ import annotations

import json
import os
import stat
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from teslausb_web.config import LightShowsSection, PathsSection, WebConfig
from teslausb_web.services.light_show_service import (
    LightShowError,
    LightShowFile,
    LightShowFileError,
    LightShowService,
    make_light_show_service,
)
from werkzeug.datastructures import FileStorage


@pytest.fixture
def light_show_folder(tmp_path: Path) -> Path:
    path = tmp_path / "LightShow"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def active_show_file(tmp_path: Path) -> Path:
    return tmp_path / "state" / "lightshow_active.json"


@pytest.fixture
def service(light_show_folder: Path, active_show_file: Path) -> LightShowService:
    return LightShowService(
        light_show_folder=light_show_folder,
        active_show_file=active_show_file,
        max_upload_size=1024,
        max_zip_size=4096,
        allowed_extensions=(".fseq", ".mp3", ".wav"),
    )


def _upload(name: str, payload: bytes) -> FileStorage:
    return FileStorage(stream=BytesIO(payload), filename=name)


def _build_zip(entries: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _read_active_state(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_list_files_returns_empty_tuple_when_folder_missing(tmp_path: Path) -> None:
    service = LightShowService(
        light_show_folder=tmp_path / "missing" / "LightShow",
        active_show_file=tmp_path / "state" / "lightshow_active.json",
        max_upload_size=1024,
        max_zip_size=4096,
        allowed_extensions=(".fseq", ".mp3", ".wav"),
    )
    assert service.list_files() == ()


def test_list_files_returns_allowed_files_sorted(
    light_show_folder: Path, service: LightShowService
) -> None:
    _write(light_show_folder / "bravo.mp3", b"b")
    _write(light_show_folder / "Alpha.fseq", b"a")
    _write(light_show_folder / "skip.txt", b"x")
    (light_show_folder / "folder.wav").mkdir()

    files = service.list_files()

    assert [entry.filename for entry in files] == ["Alpha.fseq", "bravo.mp3"]
    assert all(isinstance(entry, LightShowFile) for entry in files)
    assert files[0].modified_at.tzinfo is not None


def test_list_files_raises_typed_error_when_scanning_fails(service: LightShowService) -> None:
    with (
        patch("pathlib.Path.iterdir", side_effect=OSError("boom")),
        pytest.raises(LightShowFileError, match="Failed to list light show files"),
    ):
        service.list_files()


def test_upload_files_saves_multiple_supported_files(
    service: LightShowService,
    light_show_folder: Path,
) -> None:
    result = service.upload_files([_upload("show.fseq", b"fseq"), _upload("audio.mp3", b"mp3")])

    assert result.success is True
    assert result.file_count == 2
    assert result.message == "Successfully uploaded 2 file(s)"
    assert (light_show_folder / "show.fseq").read_bytes() == b"fseq"
    assert (light_show_folder / "audio.mp3").read_bytes() == b"mp3"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not enforced on Windows")
def test_upload_files_publishes_gadget_readable_files(
    service: LightShowService,
    light_show_folder: Path,
) -> None:
    # Regression: published files must be readable by the teslafat gadget user
    # (a different user than this web process). tempfile.mkstemp() defaults to
    # 0600, which previously made the MEDIA partition unreadable and caused a
    # deterministic NBD I/O error when the car read the light-show file.
    service.upload_files([_upload("show.fseq", b"fseq")])

    mode = stat.S_IMODE((light_show_folder / "show.fseq").stat().st_mode)
    assert mode & stat.S_IROTH, f"published file mode {mode:#o} is not other-readable"
    assert mode & stat.S_IRGRP, f"published file mode {mode:#o} is not group-readable"


def test_upload_files_returns_false_when_no_candidates(service: LightShowService) -> None:
    assert service.upload_files([]).success is False
    assert service.upload_files([_upload("", b"x")]).message == "No files selected"


def test_upload_files_collects_partial_failures(
    service: LightShowService,
    light_show_folder: Path,
) -> None:
    result = service.upload_files([_upload("ok.wav", b"wav"), _upload("bad.txt", b"txt")])

    assert result.success is True
    assert result.file_count == 1
    assert "Errors:" in result.message
    assert (light_show_folder / "ok.wav").read_bytes() == b"wav"


def test_upload_files_rejects_oversize_file(
    service: LightShowService, light_show_folder: Path
) -> None:
    result = service.upload_files([_upload("big.fseq", b"x" * 1025)])

    assert result.success is False
    assert result.file_count == 0
    assert "Limit is" in result.message
    assert not (light_show_folder / "big.fseq").exists()


def test_upload_files_overwrites_duplicate_filename(
    service: LightShowService, light_show_folder: Path
) -> None:
    first = service.upload_files([_upload("dup.wav", b"old")])
    second = service.upload_files([_upload("dup.wav", b"new")])

    assert first.success is True
    assert second.success is True
    assert (light_show_folder / "dup.wav").read_bytes() == b"new"


def test_upload_files_accepts_unicode_filename(
    service: LightShowService, light_show_folder: Path
) -> None:
    result = service.upload_files([_upload("ünicode.wav", b"hello")])

    assert result.success is True
    assert (light_show_folder / "ünicode.wav").read_bytes() == b"hello"


def test_upload_files_accepts_long_filename(
    service: LightShowService, light_show_folder: Path
) -> None:
    filename = f"{'a' * 120}.fseq"
    result = service.upload_files([_upload(filename, b"payload")])

    assert result.success is True
    assert (light_show_folder / filename).read_bytes() == b"payload"


@pytest.mark.parametrize("name", ["../../etc/passwd", "folder\\evil.wav"])
def test_upload_files_rejects_path_traversal(service: LightShowService, name: str) -> None:
    result = service.upload_files([_upload(name, b"bad")])

    assert result.success is False
    assert "Invalid filename" in result.message


def test_upload_files_raises_typed_error_on_filesystem_failure(service: LightShowService) -> None:
    with (
        patch("teslausb_web.services.light_show_service.os.replace", side_effect=OSError("boom")),
        pytest.raises(LightShowFileError, match="Failed to write"),
    ):
        service.upload_files([_upload("show.wav", b"payload")])


def test_upload_zip_extracts_nested_supported_files(
    service: LightShowService,
    light_show_folder: Path,
) -> None:
    payload = _build_zip(
        {
            "nested/folder/show.fseq": b"fseq",
            "nested/folder/audio.mp3": b"mp3",
            "ignored/readme.txt": b"txt",
        }
    )

    result = service.upload_zip(_upload("shows.zip", payload))

    assert result.success is True
    assert result.file_count == 2
    assert (light_show_folder / "show.fseq").read_bytes() == b"fseq"
    assert (light_show_folder / "audio.mp3").read_bytes() == b"mp3"


def test_upload_zip_flattens_duplicate_filenames_last_write_wins(
    service: LightShowService,
    light_show_folder: Path,
) -> None:
    payload = _build_zip(
        {
            "first/dup.wav": b"old",
            "second/dup.wav": b"new",
        }
    )

    result = service.upload_zip(_upload("shows.zip", payload))

    assert result.success is True
    assert result.file_count == 2
    assert (light_show_folder / "dup.wav").read_bytes() == b"new"


def test_upload_zip_accepts_unicode_filename(
    service: LightShowService, light_show_folder: Path
) -> None:
    payload = _build_zip({"nested/ünicode.wav": b"audio"})
    result = service.upload_zip(_upload("shows.zip", payload))
    assert result.success is True
    assert (light_show_folder / "ünicode.wav").read_bytes() == b"audio"


def test_upload_zip_rejects_oversize_zip(
    service: LightShowService, light_show_folder: Path
) -> None:
    service = LightShowService(
        light_show_folder=light_show_folder,
        active_show_file=service._active_show_file,
        max_upload_size=1024,
        max_zip_size=16,
        allowed_extensions=(".fseq", ".mp3", ".wav"),
    )

    result = service.upload_zip(_upload("shows.zip", b"x" * 17))

    assert result.success is False
    assert result.file_count == 0
    assert "ZIP file is" in result.message


def test_upload_zip_rejects_oversize_member(service: LightShowService) -> None:
    payload = _build_zip({"show.fseq": b"x" * 1025})

    result = service.upload_zip(_upload("shows.zip", payload))

    assert result.success is False
    assert result.file_count == 0
    assert "Limit is" in result.message


def test_upload_zip_rejects_empty_zip(service: LightShowService) -> None:
    result = service.upload_zip(_upload("shows.zip", _build_zip({})))
    assert result.success is False
    assert result.message == "No light show files (.fseq, .mp3, .wav) found in ZIP"


def test_upload_zip_rejects_zip_without_supported_files(service: LightShowService) -> None:
    result = service.upload_zip(_upload("shows.zip", _build_zip({"nested/readme.txt": b"x"})))
    assert result.success is False
    assert result.message == "No light show files (.fseq, .mp3, .wav) found in ZIP"


def test_upload_zip_rejects_malformed_zip(service: LightShowService) -> None:
    result = service.upload_zip(_upload("shows.zip", b"not-a-zip"))
    assert result.success is False
    assert result.message == "Invalid ZIP file"


def test_upload_zip_requires_zip_filename(service: LightShowService) -> None:
    with pytest.raises(LightShowError, match=r"Filename must end with \.zip"):
        service.upload_zip(_upload("shows.wav", b"audio"))


def test_upload_zip_raises_typed_error_on_filesystem_failure(service: LightShowService) -> None:
    payload = _build_zip({"show.fseq": b"payload"})
    with (
        patch("teslausb_web.services.light_show_service.os.replace", side_effect=OSError("boom")),
        pytest.raises(
            LightShowFileError,
            match=r"Failed to write temporary file|Failed to write .*show\.fseq",
        ),
    ):
        service.upload_zip(_upload("shows.zip", payload))


def test_delete_file_removes_existing_file(
    service: LightShowService,
    light_show_folder: Path,
) -> None:
    _write(light_show_folder / "delete.wav", b"payload")

    result = service.delete_file("delete.wav")

    assert result.success is True
    assert not (light_show_folder / "delete.wav").exists()


def test_delete_file_returns_false_for_missing_file(service: LightShowService) -> None:
    result = service.delete_file("missing.wav")
    assert result.success is False
    assert result.message == "File not found: missing.wav"


def test_delete_file_rejects_path_traversal(service: LightShowService) -> None:
    with pytest.raises(LightShowError, match="Invalid filename"):
        service.delete_file("../../etc/passwd")


def test_bulk_delete_deletes_multiple_files_and_reports_missing(
    service: LightShowService,
    light_show_folder: Path,
) -> None:
    _write(light_show_folder / "one.wav", b"1")
    _write(light_show_folder / "two.fseq", b"2")

    result = service.bulk_delete(["one.wav", "missing.mp3", "two.fseq"])

    assert result.success is True
    assert "Deleted 2 file(s). Errors:" in result.message
    assert not (light_show_folder / "one.wav").exists()
    assert not (light_show_folder / "two.fseq").exists()


def test_bulk_delete_rejects_path_traversal(service: LightShowService) -> None:
    with pytest.raises(LightShowError, match="Invalid filename"):
        service.bulk_delete(["good.wav", "../../etc/passwd"])


def test_bulk_delete_returns_false_when_no_names(service: LightShowService) -> None:
    result = service.bulk_delete([])
    assert result.success is False
    assert result.message == "No files selected"


def test_get_active_show_returns_none_when_state_missing(service: LightShowService) -> None:
    assert service.get_active_show() is None


def test_set_active_show_round_trips_with_get_active_show(
    service: LightShowService,
    light_show_folder: Path,
    active_show_file: Path,
) -> None:
    _write(light_show_folder / "show.fseq", b"payload")

    service.set_active_show("show.fseq")

    assert service.get_active_show() == "show.fseq"
    assert _read_active_state(active_show_file) == {"filename": "show.fseq"}


def test_set_active_show_rejects_missing_file(service: LightShowService) -> None:
    with pytest.raises(LightShowError, match="Light show file not found"):
        service.set_active_show("missing.fseq")


def test_set_active_show_rejects_path_traversal(service: LightShowService) -> None:
    with pytest.raises(LightShowError, match="Invalid filename"):
        service.set_active_show("../../evil.fseq")


def test_delete_file_clears_active_show_state(
    service: LightShowService,
    light_show_folder: Path,
    active_show_file: Path,
) -> None:
    _write(light_show_folder / "active.wav", b"payload")
    service.set_active_show("active.wav")

    result = service.delete_file("active.wav")

    assert result.success is True
    assert service.get_active_show() is None
    assert _read_active_state(active_show_file) == {"filename": None}


def test_get_active_show_returns_none_for_stale_state(
    service: LightShowService,
    active_show_file: Path,
) -> None:
    active_show_file.parent.mkdir(parents=True, exist_ok=True)
    active_show_file.write_text('{"filename": "missing.wav"}\n', encoding="utf-8")
    assert service.get_active_show() is None


def test_get_active_show_raises_for_malformed_state(
    service: LightShowService, active_show_file: Path
) -> None:
    active_show_file.parent.mkdir(parents=True, exist_ok=True)
    active_show_file.write_text("{not-json", encoding="utf-8")
    with pytest.raises(LightShowFileError, match="Failed to parse"):
        service.get_active_show()


def test_get_active_show_raises_for_invalid_state_filename(
    service: LightShowService,
    active_show_file: Path,
) -> None:
    active_show_file.parent.mkdir(parents=True, exist_ok=True)
    active_show_file.write_text('{"filename": "../../evil.wav"}\n', encoding="utf-8")
    with pytest.raises(LightShowFileError, match="Active show filename is invalid"):
        service.get_active_show()


def test_factory_uses_configured_paths() -> None:
    cfg = WebConfig(
        paths=PathsSection(backing_root=Path("/srv/teslausb"), state_dir=Path("/var/lib/teslausb")),
        light_shows=LightShowsSection(
            folder="CustomShows",
            active_show_relpath="custom_active.json",
            max_upload_size=4096,
            max_zip_size=8192,
            allowed_extensions=(".fseq", ".wav"),
        ),
    )

    service = make_light_show_service(cfg)

    assert service._light_show_folder == Path("/srv/teslausb") / "CustomShows"
    assert service._active_show_file == Path("/var/lib/teslausb") / "custom_active.json"
    assert service._max_upload_size == 4096
    assert service._max_zip_size == 8192
    assert service._allowed_extensions == (".fseq", ".wav")
