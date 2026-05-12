"""Tests for services.file_safety — the single doorway for protected/safe deletes.

Phase 2.1 (issue #97) introduces ``safe_delete_archive_video`` as the only
sanctioned way to delete a clip from the local archive. These tests cover
both the existing helpers (``is_protected_file``, ``safe_remove``,
``safe_rmtree``) and the new helper.

The protection contract (must never break):

* A ``*.img`` file inside ``GADGET_DIR`` MUST NEVER be deleted by any
  helper in this module, regardless of its containing directory's mtime
  or any caller policy.
* ``safe_delete_archive_video`` returns a :class:`DeleteResult` whose
  ``outcome`` distinguishes DELETED / PROTECTED / MISSING / ERROR — the
  4-state contract callers rely on for accurate accounting and
  user-facing messages without re-probing the filesystem.
"""

from __future__ import annotations

import os

import pytest

from services import file_safety
from services.file_safety import DeleteOutcome


@pytest.fixture
def reset_gadget_dir(monkeypatch, tmp_path):
    """Force file_safety to use a tmp_path-based GADGET_DIR for the test."""
    fake_gadget = tmp_path / "gadget"
    fake_gadget.mkdir()
    # Reset the lazily-cached gadget dir and patch the lookup function.
    monkeypatch.setattr(file_safety, "_gadget_dir", None)
    monkeypatch.setattr(
        file_safety, "_get_gadget_dir",
        lambda: os.path.realpath(str(fake_gadget)),
    )
    return str(fake_gadget)


# ---------------------------------------------------------------------------
# is_protected_file
# ---------------------------------------------------------------------------


class TestIsProtectedFile:
    def test_img_in_gadget_is_protected(self, reset_gadget_dir):
        path = os.path.join(reset_gadget_dir, "usb_cam.img")
        # Create the file so realpath resolves correctly.
        open(path, "wb").close()
        assert file_safety.is_protected_file(path) is True

    def test_img_outside_gadget_is_NOT_protected(self, tmp_path, reset_gadget_dir):
        path = str(tmp_path / "stranger.img")
        open(path, "wb").close()
        assert file_safety.is_protected_file(path) is False

    def test_mp4_in_gadget_is_NOT_protected(self, reset_gadget_dir):
        path = os.path.join(reset_gadget_dir, "fake.mp4")
        open(path, "wb").close()
        assert file_safety.is_protected_file(path) is False

    def test_case_insensitive_extension(self, reset_gadget_dir):
        path = os.path.join(reset_gadget_dir, "USB_CAM.IMG")
        open(path, "wb").close()
        assert file_safety.is_protected_file(path) is True

    def test_nonexistent_path_does_not_crash(self, reset_gadget_dir):
        path = os.path.join(reset_gadget_dir, "ghost.img")
        # File never created — realpath still resolves; check should still
        # return True because the path syntactically points into GADGET_DIR.
        assert file_safety.is_protected_file(path) is True


# ---------------------------------------------------------------------------
# safe_delete_archive_video — the Phase 2.1 single doorway
# ---------------------------------------------------------------------------


class TestSafeDeleteArchiveVideo:
    def test_deletes_normal_archived_clip(self, tmp_path, reset_gadget_dir):
        path = str(tmp_path / "2026-05-12_06-00-00-front.mp4")
        with open(path, "wb") as f:
            f.write(b"X" * 1024)

        result = file_safety.safe_delete_archive_video(path)

        assert result.outcome is DeleteOutcome.DELETED
        assert result.bytes_freed == 1024
        assert not os.path.exists(path)

    def test_refuses_protected_img_in_gadget(self, reset_gadget_dir):
        path = os.path.join(reset_gadget_dir, "usb_cam.img")
        with open(path, "wb") as f:
            f.write(b"X" * 1024)

        result = file_safety.safe_delete_archive_video(path)

        assert result.outcome is DeleteOutcome.PROTECTED
        assert result.bytes_freed == 0
        assert os.path.exists(path), (
            "Protected IMG file was deleted — Phase 2.1 contract violated"
        )

    def test_missing_file_returns_missing(self, tmp_path, reset_gadget_dir):
        path = str(tmp_path / "ghost.mp4")
        result = file_safety.safe_delete_archive_video(path)
        assert result.outcome is DeleteOutcome.MISSING
        assert result.bytes_freed == 0

    def test_returns_size_before_deletion(self, tmp_path, reset_gadget_dir):
        path = str(tmp_path / "clip.mp4")
        size = 4096
        with open(path, "wb") as f:
            f.write(b"X" * size)

        result = file_safety.safe_delete_archive_video(path)
        assert result.outcome is DeleteOutcome.DELETED
        assert result.bytes_freed == size

    def test_zero_byte_file_is_deleted_with_zero_bytes_freed(
        self, tmp_path, reset_gadget_dir
    ):
        # The 4-state outcome enum disambiguates "0-byte deleted file"
        # (outcome=DELETED, bytes_freed=0) from "didn't delete" — callers
        # that use ``outcome is DELETED`` (per docstring) handle this
        # correctly. The ``bytes_freed > 0`` heuristic from the original
        # API is no longer necessary; tests pin the new contract.
        path = str(tmp_path / "empty.mp4")
        open(path, "wb").close()

        result = file_safety.safe_delete_archive_video(path)
        assert result.outcome is DeleteOutcome.DELETED
        assert result.bytes_freed == 0
        assert not os.path.exists(path)

    def test_oserror_on_remove_returns_error_outcome(
        self, tmp_path, reset_gadget_dir, monkeypatch
    ):
        path = str(tmp_path / "blocked.mp4")
        with open(path, "wb") as f:
            f.write(b"X" * 100)

        def boom(_):
            raise PermissionError("EACCES")

        monkeypatch.setattr(os, "remove", boom)
        result = file_safety.safe_delete_archive_video(path)
        assert result.outcome is DeleteOutcome.ERROR
        assert result.bytes_freed == 0
        # File still exists because remove was patched.
        assert os.path.exists(path)

    def test_oserror_on_stat_returns_error_outcome(
        self, tmp_path, reset_gadget_dir, monkeypatch
    ):
        path = str(tmp_path / "weird.mp4")
        with open(path, "wb") as f:
            f.write(b"X" * 100)

        def stat_boom(_):
            raise PermissionError("stat EACCES")

        monkeypatch.setattr(os.path, "getsize", stat_boom)
        result = file_safety.safe_delete_archive_video(path)
        assert result.outcome is DeleteOutcome.ERROR
        assert result.bytes_freed == 0


# ---------------------------------------------------------------------------
# safe_remove (existing helper) — quick regression coverage
# ---------------------------------------------------------------------------


class TestSafeRemove:
    def test_removes_unprotected_file(self, tmp_path, reset_gadget_dir):
        path = str(tmp_path / "x.txt")
        open(path, "wb").close()
        assert file_safety.safe_remove(path) is True
        assert not os.path.exists(path)

    def test_refuses_protected_file(self, reset_gadget_dir):
        path = os.path.join(reset_gadget_dir, "u.img")
        open(path, "wb").close()
        assert file_safety.safe_remove(path) is False
        assert os.path.exists(path)

    def test_missing_file_returns_false(self, tmp_path, reset_gadget_dir):
        assert file_safety.safe_remove(str(tmp_path / "ghost")) is False


# ---------------------------------------------------------------------------
# safe_rmtree (existing helper) — quick regression coverage
# ---------------------------------------------------------------------------


class TestSafeRmtree:
    def test_removes_clean_tree(self, tmp_path, reset_gadget_dir):
        d = tmp_path / "tree"
        d.mkdir()
        (d / "a.txt").write_bytes(b"x")
        (d / "b.txt").write_bytes(b"y")
        assert file_safety.safe_rmtree(str(d)) is True
        assert not d.exists()

    def test_refuses_tree_containing_protected_file(self, reset_gadget_dir):
        # Create a subtree containing a protected .img file. The helper
        # MUST refuse to remove the parent — both the tree and the file
        # must still exist after the call.
        sub = os.path.join(reset_gadget_dir, "sub")
        os.makedirs(sub)
        img_path = os.path.join(sub, "u.img")
        open(img_path, "wb").close()
        assert file_safety.safe_rmtree(sub) is False
        assert os.path.exists(img_path)
        assert os.path.isdir(sub)

    def test_missing_dir_returns_false(self, tmp_path, reset_gadget_dir):
        assert file_safety.safe_rmtree(str(tmp_path / "ghost")) is False
