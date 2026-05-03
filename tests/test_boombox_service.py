"""Tests for boombox_service — upload validation, delete, and list."""

import io
import os
import pytest
from unittest.mock import patch, MagicMock

# boombox_service is imported via scripts/web path added in conftest.py
from services.boombox_service import (
    _sanitize_filename,
    _validate_filename,
    _safe_file_path,
    BoomboxServiceError,
    MAX_FILE_SIZE,
    MAX_FILE_COUNT,
)


# ---------------------------------------------------------------------------
# _sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_strips_directory_components(self):
        assert _sanitize_filename("../../etc/passwd.mp3") == "passwd.mp3"

    def test_strips_null_bytes(self):
        assert _sanitize_filename("sound\x00.mp3") == "sound.mp3"

    def test_collapses_whitespace(self):
        assert _sanitize_filename("my  song.mp3") == "my song.mp3"

    def test_empty_string(self):
        assert _sanitize_filename("") == ""

    def test_strips_backslash(self):
        # On Unix, backslash is not a path separator, so basename keeps the whole
        # string; the regex then removes the backslash character itself.
        assert _sanitize_filename("folder\\file.mp3") == "folderfile.mp3"


# ---------------------------------------------------------------------------
# _validate_filename
# ---------------------------------------------------------------------------

class TestValidateFilename:
    def test_accepts_mp3(self):
        assert _validate_filename("honk.mp3") == "honk.mp3"

    def test_accepts_mp3_uppercase(self):
        assert _validate_filename("HONK.MP3") == "HONK.MP3"

    def test_rejects_wav(self):
        with pytest.raises(BoomboxServiceError, match="Only MP3"):
            _validate_filename("honk.wav")

    def test_rejects_flac(self):
        with pytest.raises(BoomboxServiceError, match="Only MP3"):
            _validate_filename("honk.flac")

    def test_rejects_empty(self):
        with pytest.raises(BoomboxServiceError, match="Invalid filename"):
            _validate_filename("")

    def test_rejects_traversal(self):
        """Traversal characters are stripped, leaving a valid name."""
        result = _validate_filename("../../../evil.mp3")
        assert result == "evil.mp3"

    def test_rejects_no_ext(self):
        with pytest.raises(BoomboxServiceError, match="Only MP3"):
            _validate_filename("noextension")


# ---------------------------------------------------------------------------
# _safe_file_path
# ---------------------------------------------------------------------------

class TestSafeFilePath:
    def test_returns_path_within_media_dir(self, tmp_path):
        media_dir = str(tmp_path)
        result = _safe_file_path(media_dir, "honk.mp3")
        assert result == os.path.join(media_dir, "honk.mp3")

    def test_rejects_traversal(self, tmp_path):
        media_dir = str(tmp_path)
        with pytest.raises(BoomboxServiceError, match="Invalid file path"):
            _safe_file_path(media_dir, "../outside.mp3")


# ---------------------------------------------------------------------------
# upload_boombox_file — size limit
# ---------------------------------------------------------------------------

class FakeFileStorage:
    """Minimal werkzeug FileStorage substitute for tests."""

    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self.stream = io.BytesIO(content)

    def read(self) -> bytes:
        return self.stream.getvalue()


class TestUploadSizeLimit:
    def test_rejects_oversized_file(self):
        oversized = b"x" * (MAX_FILE_SIZE + 1)
        fs = FakeFileStorage("big.mp3", oversized)
        with patch("services.boombox_service.current_mode", return_value="edit"), \
             patch("services.boombox_service._ensure_mount",
                   return_value=("/fake/mount", "")):
            ok, msg = __import__(
                "services.boombox_service", fromlist=["upload_boombox_file"]
            ).upload_boombox_file(fs)
            assert not ok
            assert "too large" in msg.lower()

    def test_rejects_wrong_extension(self):
        fs = FakeFileStorage("sound.wav", b"RIFF")
        with pytest.raises(BoomboxServiceError, match="Only MP3"):
            from services.boombox_service import upload_boombox_file
            upload_boombox_file(fs)


# ---------------------------------------------------------------------------
# upload_boombox_file — file count limit
# ---------------------------------------------------------------------------

class TestUploadCountLimit:
    def test_rejects_when_full(self, tmp_path):
        """Upload is rejected when MAX_FILE_COUNT files already exist."""
        media_dir = tmp_path / "Media"
        media_dir.mkdir()
        # Create MAX_FILE_COUNT dummy MP3 files
        for i in range(MAX_FILE_COUNT):
            (media_dir / f"sound{i}.mp3").write_bytes(b"ID3")

        fs = FakeFileStorage("new.mp3", b"ID3")

        def fake_ensure_mount():
            return str(tmp_path), ""

        def fake_media_dir(mount_path):
            return str(media_dir)

        with patch("services.boombox_service.current_mode", return_value="edit"), \
             patch("services.boombox_service._ensure_mount", fake_ensure_mount), \
             patch("services.boombox_service._media_dir", fake_media_dir):
            from services.boombox_service import upload_boombox_file
            ok, msg = upload_boombox_file(fs)
            assert not ok
            assert "Maximum" in msg


# ---------------------------------------------------------------------------
# delete_boombox_file — safety checks
# ---------------------------------------------------------------------------

class TestDeleteBoomboxFile:
    def test_rejects_nonexistent_file(self, tmp_path):
        media_dir = tmp_path / "Media"
        media_dir.mkdir()

        def fake_ensure_mount():
            return str(tmp_path), ""

        def fake_media_dir(mount_path):
            return str(media_dir)

        with patch("services.boombox_service.current_mode", return_value="edit"), \
             patch("services.boombox_service._ensure_mount", fake_ensure_mount), \
             patch("services.boombox_service._media_dir", fake_media_dir):
            from services.boombox_service import delete_boombox_file
            ok, msg = delete_boombox_file("ghost.mp3")
            assert not ok
            assert "not found" in msg.lower()

    def test_rejects_wrong_extension(self, tmp_path):
        with pytest.raises(BoomboxServiceError, match="Only MP3"):
            from services.boombox_service import delete_boombox_file
            delete_boombox_file("photo.png")

    def test_deletes_existing_file(self, tmp_path):
        media_dir = tmp_path / "Media"
        media_dir.mkdir()
        (media_dir / "honk.mp3").write_bytes(b"ID3")

        def fake_ensure_mount():
            return str(tmp_path), ""

        def fake_media_dir(mount_path):
            return str(media_dir)

        with patch("services.boombox_service.current_mode", return_value="edit"), \
             patch("services.boombox_service._ensure_mount", fake_ensure_mount), \
             patch("services.boombox_service._media_dir", fake_media_dir), \
             patch("services.boombox_service.close_samba_share"):
            from services.boombox_service import delete_boombox_file
            ok, msg = delete_boombox_file("honk.mp3")
            assert ok
            assert "honk.mp3" in msg
            assert not (media_dir / "honk.mp3").exists()


# ---------------------------------------------------------------------------
# list_boombox_files
# ---------------------------------------------------------------------------

class TestListBoomboxFiles:
    def test_lists_only_mp3_files(self, tmp_path):
        media_dir = tmp_path / "Media"
        media_dir.mkdir()
        (media_dir / "a.mp3").write_bytes(b"ID3")
        (media_dir / "b.wav").write_bytes(b"RIFF")
        (media_dir / "c.mp3").write_bytes(b"ID3x")

        def fake_ensure_mount():
            return str(tmp_path), ""

        def fake_media_dir(mount_path):
            return str(media_dir)

        with patch("services.boombox_service._ensure_mount", fake_ensure_mount), \
             patch("services.boombox_service._media_dir", fake_media_dir):
            from services.boombox_service import list_boombox_files
            files, error, _, _ = list_boombox_files()
            names = [f["name"] for f in files]
            assert "a.mp3" in names
            assert "c.mp3" in names
            assert "b.wav" not in names
            assert error == ""

    def test_returns_sorted_names(self, tmp_path):
        media_dir = tmp_path / "Media"
        media_dir.mkdir()
        for name in ["zebra.mp3", "apple.mp3", "mango.mp3"]:
            (media_dir / name).write_bytes(b"ID3")

        def fake_ensure_mount():
            return str(tmp_path), ""

        def fake_media_dir(mount_path):
            return str(media_dir)

        with patch("services.boombox_service._ensure_mount", fake_ensure_mount), \
             patch("services.boombox_service._media_dir", fake_media_dir):
            from services.boombox_service import list_boombox_files
            files, _, _, _ = list_boombox_files()
            names = [f["name"] for f in files]
            assert names == sorted(names, key=str.lower)

    def test_returns_error_when_mount_missing(self):
        with patch("services.boombox_service._ensure_mount",
                   return_value=("", "Music drive not mounted.")):
            from services.boombox_service import list_boombox_files
            files, error, _, _ = list_boombox_files()
            assert files == []
            assert "not mounted" in error
