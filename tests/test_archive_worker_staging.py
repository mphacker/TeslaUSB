"""Tests for issue #184 Wave 3 — Phase H (atomic-copy staging dir).

Covers the new ``<archive_root>/.staging/`` layout for in-flight
``.partial`` files. Pre-Wave-3 archive_worker tests already exercise
the legacy fallback (no ``staging_root`` arg → ``dest + '.partial'``);
this file extends the coverage to the new staging-dir mode that
production now uses.
"""

from __future__ import annotations

import os

import pytest

from services import archive_worker


_FTYP_HEADER = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
_MOOV_HEADER = b"\x00\x00\x00\x10moov\x00\x00\x00\x00\x00\x00\x00\x00"
_MDAT_HEADER = b"\x00\x00\x00\x10mdat\x00\x00\x00\x00\x00\x00\x00\x00"


def _write_valid_mp4(path: str, payload: bytes = b"frame_data" * 64) -> None:
    """Write the smallest possible MP4 that ``_atomic_copy``'s
    moov/mdat verifier accepts."""
    with open(path, 'wb') as f:
        f.write(_FTYP_HEADER)
        f.write(_MOOV_HEADER)
        # mdat header + payload
        size = 8 + len(payload)
        f.write(size.to_bytes(4, 'big') + b'mdat' + payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestStagingPathHelpers:
    def test_staging_root_resolves_under_archive(self, tmp_path):
        root = str(tmp_path / "ArchivedClips")
        result = archive_worker._staging_root(root)
        assert result == os.path.join(root, '.staging')

    def test_staging_partial_path_is_unique_per_dest(self, tmp_path):
        root = str(tmp_path / "ArchivedClips")
        d1 = os.path.join(root, '2026-05-04_08-00-00', 'front.mp4')
        d2 = os.path.join(root, '2026-05-05_09-00-00', 'front.mp4')
        p1 = archive_worker._staging_partial_path(root, d1)
        p2 = archive_worker._staging_partial_path(root, d2)
        # Same basename — but the hash prefix MUST differ so two
        # in-flight copies of differently-located ``front.mp4`` clips
        # cannot clobber each other in the staging dir.
        assert p1 != p2
        assert os.path.basename(p1).endswith('-front.mp4.partial')
        assert os.path.basename(p2).endswith('-front.mp4.partial')

    def test_staging_partial_path_is_stable_for_same_dest(self, tmp_path):
        root = str(tmp_path / "ArchivedClips")
        d = os.path.join(root, 'sub', 'clip.mp4')
        p1 = archive_worker._staging_partial_path(root, d)
        p2 = archive_worker._staging_partial_path(root, d)
        assert p1 == p2


# ---------------------------------------------------------------------------
# _atomic_copy with staging_root — partials live in .staging, not
# the destination directory.
# ---------------------------------------------------------------------------

class TestAtomicCopyWithStaging:
    def test_partial_lands_in_staging_dir_not_dest(self, tmp_path,
                                                   monkeypatch):
        archive = tmp_path / "ArchivedClips"
        archive.mkdir()
        staging = archive_worker._staging_root(str(archive))

        sub = archive / "2026-05-04_08-00-00"
        sub.mkdir()
        src = tmp_path / "src.mp4"
        _write_valid_mp4(str(src))
        dest = sub / "front.mp4"

        # Spy on the open call to capture the partial filename.
        seen_paths = []
        real_open = archive_worker.open if hasattr(archive_worker, 'open') else open

        def spy_open(p, mode='r', *a, **kw):
            seen_paths.append((p, mode))
            return real_open(p, mode, *a, **kw)

        monkeypatch.setattr('builtins.open', spy_open)
        try:
            size = archive_worker._atomic_copy(
                str(src), str(dest), chunk_size=4096,
                staging_root=staging,
            )
        finally:
            monkeypatch.setattr('builtins.open', real_open)

        assert size > 0
        # Final dest exists.
        assert os.path.isfile(str(dest))
        # No .partial in the dest tree.
        for entry in os.listdir(str(sub)):
            assert not entry.endswith('.partial')
        # Staging dir was created.
        assert os.path.isdir(staging)
        # At least one open call wrote into the staging dir.
        write_targets = [p for p, m in seen_paths if 'w' in m]
        assert any(staging in p for p in write_targets), (
            f"expected a write inside {staging}, saw {write_targets}"
        )

    def test_legacy_mode_unchanged_when_staging_root_none(self, tmp_path):
        # Back-compat: without ``staging_root`` the partial still lands
        # at ``dest + '.partial'`` so the existing test suite passes.
        archive = tmp_path / "ArchivedClips"
        archive.mkdir()
        sub = archive / "subdir"
        sub.mkdir()
        src = tmp_path / "src.mp4"
        _write_valid_mp4(str(src))
        dest = sub / "front.mp4"

        size = archive_worker._atomic_copy(
            str(src), str(dest), chunk_size=4096,
        )
        assert size > 0
        assert os.path.isfile(str(dest))
        # No .partial leftover anywhere.
        assert not (sub / "front.mp4.partial").exists()


# ---------------------------------------------------------------------------
# _sweep_partial_orphans — staging scandir + legacy-tree fallback.
# ---------------------------------------------------------------------------

class TestSweepPartialOrphansStagingMode:
    def test_sweep_removes_orphans_from_staging_dir(self, tmp_path):
        archive = tmp_path / "ArchivedClips"
        archive.mkdir()
        staging = archive_worker._staging_root(str(archive))
        os.makedirs(staging, exist_ok=True)
        # Three orphans in staging from a prior crash.
        for i in range(3):
            with open(os.path.join(staging, f"abc{i}-clip.mp4.partial"), 'wb') as f:
                f.write(b"x" * 64)
        # A non-partial in staging must NOT be touched (defensive —
        # production never puts non-partials there but the sweeper
        # shouldn't decide policy).
        keep = os.path.join(staging, "keep.txt")
        with open(keep, 'w') as f:
            f.write("metadata")
        removed = archive_worker._sweep_partial_orphans(str(archive))
        assert removed == 3
        assert os.path.isfile(keep)
        for entry in os.listdir(staging):
            assert entry == "keep.txt"

    def test_sweep_combines_staging_and_legacy_orphans(self, tmp_path):
        archive = tmp_path / "ArchivedClips"
        archive.mkdir()
        staging = archive_worker._staging_root(str(archive))
        os.makedirs(staging, exist_ok=True)
        # One orphan in staging (Wave 3+ leftover).
        with open(os.path.join(staging, "abc-new.mp4.partial"), 'wb') as f:
            f.write(b"a")
        # One legacy orphan inside an event dir (pre-Wave-3 leftover).
        sub = archive / "2026-05-11_14-44-00"
        sub.mkdir()
        with open(os.path.join(str(sub), "old.mp4.partial"), 'wb') as f:
            f.write(b"b")
        removed = archive_worker._sweep_partial_orphans(str(archive))
        assert removed == 2
        assert not os.path.isfile(os.path.join(staging, "abc-new.mp4.partial"))
        assert not os.path.isfile(os.path.join(str(sub), "old.mp4.partial"))

    def test_sweep_skips_non_partial_files_in_staging(self, tmp_path):
        archive = tmp_path / "ArchivedClips"
        archive.mkdir()
        staging = archive_worker._staging_root(str(archive))
        os.makedirs(staging, exist_ok=True)
        # A bare .mp4 in staging is a defensive corner case — must NOT
        # be deleted by the orphan sweep.
        keep = os.path.join(staging, "real.mp4")
        with open(keep, 'wb') as f:
            f.write(b"keep")
        removed = archive_worker._sweep_partial_orphans(str(archive))
        assert removed == 0
        assert os.path.isfile(keep)
