"""Tests for Phase 2.7 — cloud_synced_files.file_path canonicalization.

Pre-2.7 the cloud_synced_files table stored ``file_path`` in inconsistent
forms across writers:

* The bulk worker wrote relative POSIX (``ArchivedClips/foo.mp4``,
  ``SentryClips/2026-01-01_10-00-00``).
* :func:`cloud_archive_service.queue_event_for_sync` wrote absolute
  filesystem paths from ``os.scandir().path`` (e.g.
  ``/mnt/gadget/part1-ro/TeslaCam/SentryClips/2026-01-01_10-00-00``).
* :func:`cloud_archive_service._reconcile_with_remote` mostly stripped
  trailing slashes from ``rclone lsf`` output but missed one branch,
  producing corrupt rows like ``ArchivedClips/foo.mp4/``.

The mismatch broke dedup checks across writers and meant a row queued via
the UI button could never be matched against the bulk worker's row of the
same event.

Phase 2.7 introduces:

1. A pure :func:`canonical_cloud_path` helper that normalises any input
   form (absolute or relative; with or without trailing slashes; with
   Windows backslashes; with redundant ``./`` or ``//``) to a single
   canonical relative POSIX path beneath the well-known TeslaCam roots.
2. A one-shot v2 schema migration that rewrites every existing
   ``cloud_synced_files`` row to canonical form, snapshotting the DB
   first and merging duplicate rows by status priority.
3. Defensive wrapping of every INSERT / SELECT / UPDATE / DELETE
   ``file_path`` site so future writers can never reintroduce the
   mismatch.

These tests cover the helper, the migration (including the duplicate
merge logic), the snapshot, idempotence, and the dedup fix in
``queue_event_for_sync``.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from unittest import mock

import pytest

from services import cloud_archive_service as svc
from services.cloud_archive_service import canonical_cloud_path


# ---------------------------------------------------------------------------
# canonical_cloud_path: pure-function unit tests
# ---------------------------------------------------------------------------


class TestCanonicalCloudPathBasics:
    """Forms that already match the canonical contract pass through."""

    def test_empty_string_passthrough(self):
        assert canonical_cloud_path("") == ""

    def test_none_returns_falsy(self):
        # canonical_cloud_path treats falsy inputs as a no-op so callers
        # don't need a guard. We don't care if it returns the input or
        # an empty string — only that it doesn't raise.
        result = canonical_cloud_path(None)  # type: ignore[arg-type]
        assert not result

    def test_already_canonical_archived(self):
        assert canonical_cloud_path("ArchivedClips/2026-01-01-front.mp4") == \
            "ArchivedClips/2026-01-01-front.mp4"

    def test_already_canonical_sentry_event(self):
        assert canonical_cloud_path("SentryClips/2026-01-01_10-00-00") == \
            "SentryClips/2026-01-01_10-00-00"

    def test_already_canonical_saved_event(self):
        assert canonical_cloud_path("SavedClips/2026-02-15_18-30-45") == \
            "SavedClips/2026-02-15_18-30-45"

    def test_root_only_passthrough(self):
        # Just the root segment alone is technically valid — preserve it.
        assert canonical_cloud_path("ArchivedClips") == "ArchivedClips"


class TestCanonicalCloudPathAbsoluteStripping:
    """Absolute paths under known roots have everything before the root
    stripped."""

    def test_archive_dir_absolute(self):
        # The local archive lives at ~pi/ArchivedClips; the canonical
        # form drops everything up to and including the path component
        # before the root.
        assert canonical_cloud_path("/home/pi/ArchivedClips/2026-01-01-front.mp4") == \
            "ArchivedClips/2026-01-01-front.mp4"

    def test_present_mode_sentry_absolute(self):
        # /mnt/gadget/part1-ro/TeslaCam/SentryClips/<event> — the
        # form actually written by queue_event_for_sync's
        # ``os.scandir().path``.
        assert canonical_cloud_path(
            "/mnt/gadget/part1-ro/TeslaCam/SentryClips/2026-01-01_10-00-00"
        ) == "SentryClips/2026-01-01_10-00-00"

    def test_edit_mode_sentry_absolute(self):
        # /mnt/gadget/part1/TeslaCam/SentryClips/... (RW mount form).
        assert canonical_cloud_path(
            "/mnt/gadget/part1/TeslaCam/SentryClips/2026-01-01_10-00-00"
        ) == "SentryClips/2026-01-01_10-00-00"

    def test_saved_clips_absolute(self):
        assert canonical_cloud_path(
            "/mnt/gadget/part1-ro/TeslaCam/SavedClips/2026-02-15_18-30-45"
        ) == "SavedClips/2026-02-15_18-30-45"

    def test_recent_clips_absolute(self):
        assert canonical_cloud_path(
            "/mnt/gadget/part1-ro/TeslaCam/RecentClips/2026-03-01_09-15-00-front.mp4"
        ) == "RecentClips/2026-03-01_09-15-00-front.mp4"

    def test_track_mode_absolute(self):
        assert canonical_cloud_path(
            "/mnt/gadget/part1-ro/TeslaCam/TeslaTrackMode/session1"
        ) == "TeslaTrackMode/session1"

    def test_event_with_video_file(self):
        # Some absolute paths are deeper than just the event dir —
        # /mnt/.../SentryClips/<event>/front.mp4.
        assert canonical_cloud_path(
            "/mnt/gadget/part1-ro/TeslaCam/SentryClips/2026-01-01_10-00-00/front.mp4"
        ) == "SentryClips/2026-01-01_10-00-00/front.mp4"


class TestCanonicalCloudPathSlashes:
    """Slash defects: leading, trailing, doubled, and Windows backslashes."""

    def test_strip_trailing_slash(self):
        # The exact production-corruption pattern that motivated 2.7.
        assert canonical_cloud_path("ArchivedClips/2026-04-07_13-56-53-back.mp4/") == \
            "ArchivedClips/2026-04-07_13-56-53-back.mp4"

    def test_strip_double_trailing_slash(self):
        assert canonical_cloud_path("ArchivedClips/foo.mp4//") == \
            "ArchivedClips/foo.mp4"

    def test_strip_leading_slash(self):
        assert canonical_cloud_path("/ArchivedClips/foo.mp4") == \
            "ArchivedClips/foo.mp4"

    def test_collapse_double_slash(self):
        assert canonical_cloud_path("ArchivedClips//foo.mp4") == \
            "ArchivedClips/foo.mp4"

    def test_collapse_triple_slash(self):
        assert canonical_cloud_path("ArchivedClips///foo.mp4") == \
            "ArchivedClips/foo.mp4"

    def test_windows_backslashes(self):
        assert canonical_cloud_path("ArchivedClips\\foo.mp4") == \
            "ArchivedClips/foo.mp4"

    def test_mixed_separators(self):
        assert canonical_cloud_path("ArchivedClips\\sub/foo.mp4") == \
            "ArchivedClips/sub/foo.mp4"

    def test_dot_components_collapsed(self):
        assert canonical_cloud_path("ArchivedClips/./foo.mp4") == \
            "ArchivedClips/foo.mp4"

    def test_only_dot_returns_empty(self):
        # posixpath.normpath('.') is '.'; we map that to ''.
        assert canonical_cloud_path(".") == ""


class TestCanonicalCloudPathRejectsTraversal:
    """``..`` segments are rejected with ``ValueError`` BEFORE
    posixpath.normpath has a chance to silently collapse them. This
    matters because ``remove_from_queue`` is reachable from raw user
    input via the cloud_archive blueprint.
    """

    def test_dotdot_in_middle_raises(self):
        with pytest.raises(ValueError, match="traversal"):
            canonical_cloud_path("ArchivedClips/../etc/passwd")

    def test_dotdot_at_start_raises(self):
        with pytest.raises(ValueError, match="traversal"):
            canonical_cloud_path("../ArchivedClips/foo.mp4")

    def test_dotdot_in_absolute_path_raises(self):
        # The classic exploit: /a/../b becomes /b after normpath,
        # potentially letting an attacker reference a row they
        # shouldn't be able to address. Must raise.
        with pytest.raises(ValueError, match="traversal"):
            canonical_cloud_path(
                "/home/pi/ArchivedClips/../../../etc/passwd"
            )

    def test_dotdot_basename_substring_allowed(self):
        # A literal '..' inside a basename (e.g. 'foo..bar.mp4') is
        # NOT a traversal — the segment check only matches '..' as a
        # full path component.
        assert canonical_cloud_path("ArchivedClips/foo..bar.mp4") == \
            "ArchivedClips/foo..bar.mp4"

    def test_single_dot_basename_allowed(self):
        # 'a.b' is fine; only the literal segment '..' is rejected.
        assert canonical_cloud_path("ArchivedClips/a.b.mp4") == \
            "ArchivedClips/a.b.mp4"

    def test_dotdot_at_end_raises(self):
        with pytest.raises(ValueError, match="traversal"):
            canonical_cloud_path("ArchivedClips/foo/..")


class TestCanonicalCloudPathUnknownRoots:
    """Paths that don't contain a known root segment are still cleaned
    of the leading slash and trailing slash, but otherwise preserved."""

    def test_unknown_root_keeps_relative(self):
        # No known root: this should not crash but also shouldn't try to
        # invent one. The path just gets cleaned.
        assert canonical_cloud_path("/some/random/path.mp4") == \
            "some/random/path.mp4"

    def test_unknown_root_keeps_subdir(self):
        assert canonical_cloud_path("custom/folder/clip.mp4") == \
            "custom/folder/clip.mp4"

    def test_basename_substring_doesnt_match(self):
        # 'someArchivedClipsthing.mp4' contains the substring
        # 'ArchivedClips' but is not actually under the root. The
        # canonical helper must NOT match substrings — only path
        # segments. We use find('/<root>/') in the implementation, so a
        # bare basename without surrounding slashes does NOT match.
        # Result: leading slash stripped, otherwise unchanged.
        assert canonical_cloud_path(
            "/home/pi/someArchivedClipsthing.mp4"
        ) == "home/pi/someArchivedClipsthing.mp4"


# ---------------------------------------------------------------------------
# v2 migration: cloud_synced_files row rewriting
# ---------------------------------------------------------------------------


def _seed_cloud_db(db_path, rows):
    """Create a v1-shaped DB and INSERT the given (file_path, status,
    retry_count) tuples. Returns the connection (caller closes)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE cloud_synced_files (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               file_path TEXT NOT NULL UNIQUE,
               file_size INTEGER,
               file_mtime REAL,
               remote_path TEXT,
               status TEXT DEFAULT 'pending',
               synced_at TEXT,
               retry_count INTEGER DEFAULT 0,
               last_error TEXT
           )"""
    )
    for fp, status, retry in rows:
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES (?, ?, ?)",
            (fp, status, retry),
        )
    conn.commit()
    return conn


class TestMigrationCanonicalizesPaths:
    """v2 migration rewrites mixed-form rows to canonical form."""

    def test_absolute_path_rewritten(self, tmp_path):
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("/mnt/gadget/part1-ro/TeslaCam/SentryClips/event_a", "pending", 0),
        ])
        try:
            rewrites, merges = svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert rewrites == 1
            assert merges == 0
            row = conn.execute(
                "SELECT file_path FROM cloud_synced_files"
            ).fetchone()
            assert row[0] == "SentryClips/event_a"
        finally:
            conn.close()

    def test_trailing_slash_rewritten(self, tmp_path):
        # The exact production corruption row.
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("ArchivedClips/2026-04-07_13-56-53-back.mp4/", "synced", 0),
        ])
        try:
            rewrites, _ = svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert rewrites == 1
            row = conn.execute(
                "SELECT file_path FROM cloud_synced_files"
            ).fetchone()
            assert row[0] == "ArchivedClips/2026-04-07_13-56-53-back.mp4"
        finally:
            conn.close()

    def test_canonical_rows_unchanged(self, tmp_path):
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("ArchivedClips/clip_a.mp4", "synced", 0),
            ("SentryClips/event_b", "pending", 1),
            ("SavedClips/event_c", "failed", 3),
        ])
        try:
            rewrites, merges = svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert rewrites == 0
            assert merges == 0
            paths = sorted(
                r[0] for r in conn.execute(
                    "SELECT file_path FROM cloud_synced_files"
                )
            )
            assert paths == [
                "ArchivedClips/clip_a.mp4",
                "SavedClips/event_c",
                "SentryClips/event_b",
            ]
        finally:
            conn.close()

    def test_mixed_batch(self, tmp_path):
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("ArchivedClips/already_canonical.mp4", "synced", 0),
            ("/home/pi/ArchivedClips/abs_archive.mp4", "pending", 0),
            ("/mnt/gadget/part1-ro/TeslaCam/SentryClips/abs_sentry", "pending", 2),
            ("SavedClips/saved_canonical/", "failed", 4),
        ])
        try:
            rewrites, merges = svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert rewrites == 3  # three needed rewriting
            assert merges == 0  # no collisions
            paths = sorted(
                r[0] for r in conn.execute(
                    "SELECT file_path FROM cloud_synced_files"
                )
            )
            assert paths == [
                "ArchivedClips/abs_archive.mp4",
                "ArchivedClips/already_canonical.mp4",
                "SavedClips/saved_canonical",
                "SentryClips/abs_sentry",
            ]
        finally:
            conn.close()


class TestMigrationDuplicateMerging:
    """When two rows collapse to the same canonical path, the higher-
    priority status wins."""

    def test_synced_beats_pending(self, tmp_path):
        # Synced was inserted first (legacy bulk worker), pending added
        # later by queue_event_for_sync — both refer to the same event
        # but the synced form is canonical and the pending form is
        # absolute. After migration we should keep the synced row.
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("SentryClips/event_x", "synced", 0),
            ("/mnt/gadget/part1-ro/TeslaCam/SentryClips/event_x", "pending", 0),
        ])
        try:
            rewrites, merges = svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert merges == 1
            rows = conn.execute(
                "SELECT file_path, status FROM cloud_synced_files"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "SentryClips/event_x"
            assert rows[0][1] == "synced"
        finally:
            conn.close()

    def test_synced_beats_dead_letter(self, tmp_path):
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("/mnt/gadget/part1-ro/TeslaCam/SentryClips/event_y", "dead_letter", 5),
            ("SentryClips/event_y", "synced", 0),
        ])
        try:
            _, merges = svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert merges == 1
            rows = conn.execute(
                "SELECT file_path, status FROM cloud_synced_files"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][1] == "synced"
        finally:
            conn.close()

    def test_dead_letter_beats_failed(self, tmp_path):
        # Dead-letter is the operator's "give up" decision — demoting it
        # back to failed would re-enqueue an upload that has already
        # exhausted its retries.
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("ArchivedClips/clip_z.mp4", "failed", 3),
            ("/home/pi/ArchivedClips/clip_z.mp4", "dead_letter", 5),
        ])
        try:
            _, merges = svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert merges == 1
            rows = conn.execute(
                "SELECT file_path, status FROM cloud_synced_files"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][1] == "dead_letter"
        finally:
            conn.close()

    def test_failed_beats_pending(self, tmp_path):
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("ArchivedClips/clip_w.mp4", "pending", 0),
            ("/home/pi/ArchivedClips/clip_w.mp4", "failed", 2),
        ])
        try:
            _, merges = svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert merges == 1
            rows = conn.execute(
                "SELECT file_path, status FROM cloud_synced_files"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][1] == "failed"
        finally:
            conn.close()


class TestMigrationSnapshot:
    """The DB is snapshotted to ``.bak.v2-canonical-paths`` BEFORE any
    writes. A power-loss mid-migration leaves both files on disk."""

    def test_backup_file_created(self, tmp_path):
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("/home/pi/ArchivedClips/clip.mp4", "pending", 0),
        ])
        try:
            assert not (tmp_path / "cloud.db.bak.v2-canonical-paths").exists()
            svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert (tmp_path / "cloud.db.bak.v2-canonical-paths").exists()
        finally:
            conn.close()

    def test_backup_preserves_original_rows(self, tmp_path):
        # The backup is taken before any UPDATEs; opening it should
        # reveal the un-canonicalized form.
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("/home/pi/ArchivedClips/clip.mp4", "pending", 0),
        ])
        try:
            svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
        finally:
            conn.close()

        # Live DB is canonicalized.
        live = sqlite3.connect(str(db))
        try:
            paths = [r[0] for r in live.execute(
                "SELECT file_path FROM cloud_synced_files"
            )]
            assert paths == ["ArchivedClips/clip.mp4"]
        finally:
            live.close()

        # Backup retains original.
        backup = sqlite3.connect(str(tmp_path / "cloud.db.bak.v2-canonical-paths"))
        try:
            paths = [r[0] for r in backup.execute(
                "SELECT file_path FROM cloud_synced_files"
            )]
            assert paths == ["/home/pi/ArchivedClips/clip.mp4"]
        finally:
            backup.close()

    def test_backup_not_overwritten_on_repeat(self, tmp_path):
        # If the migration is re-run (e.g. after a partial crash where
        # rewrites started but version wasn't bumped), we must NOT
        # overwrite the existing backup with a half-migrated copy.
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("/home/pi/ArchivedClips/clip_a.mp4", "pending", 0),
        ])
        try:
            svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
        finally:
            conn.close()

        backup_path = tmp_path / "cloud.db.bak.v2-canonical-paths"
        first_mtime = backup_path.stat().st_mtime

        # Insert new (already-canonical) row, then re-run.
        conn2 = sqlite3.connect(str(db))
        try:
            conn2.execute(
                "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
                "VALUES (?, ?, ?)",
                ("ArchivedClips/clip_b.mp4", "pending", 0),
            )
            conn2.commit()
            # Sleep 1.1s so any new copy would have a different mtime.
            import time
            time.sleep(1.1)
            svc._migrate_canonicalize_paths_v2(conn2, str(db))
            conn2.commit()
        finally:
            conn2.close()

        # Backup mtime must not have changed.
        assert backup_path.stat().st_mtime == first_mtime


class TestMigrationIdempotent:
    """Running the migration twice on already-canonical data is a no-op."""

    def test_second_run_no_changes(self, tmp_path):
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("ArchivedClips/clip.mp4", "synced", 0),
            ("SentryClips/event_a", "pending", 0),
        ])
        try:
            r1, m1 = svc._migrate_canonicalize_paths_v2(conn, str(db))
            r2, m2 = svc._migrate_canonicalize_paths_v2(conn, str(db))
            conn.commit()
            assert r1 == 0 and m1 == 0
            assert r2 == 0 and m2 == 0
        finally:
            conn.close()

    def test_empty_db(self, tmp_path):
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [])
        try:
            r, m = svc._migrate_canonicalize_paths_v2(conn, str(db))
            assert r == 0
            assert m == 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# queue_event_for_sync: dedup against the bulk worker's canonical row
# ---------------------------------------------------------------------------


class TestQueueEventForSyncCanonicalDedup:
    """The dedup check + INSERT in ``queue_event_for_sync`` must use the
    canonical form so it matches the bulk worker's row."""

    def test_dedup_skips_already_synced_canonical_row(self, tmp_path, monkeypatch):
        # Set up a fake TeslaCam tree on disk so os.scandir finds files.
        teslacam = tmp_path / "TeslaCam"
        sentry = teslacam / "SentryClips" / "event_q"
        sentry.mkdir(parents=True)
        # File name MUST contain the event name — queue_event_for_sync
        # filters with ``event_name in entry.name`` (Tesla writes per-
        # camera files prefixed with the event timestamp).
        clip = sentry / "event_q-front.mp4"
        clip.write_bytes(b"x" * 100)

        # Patch get_teslacam_path to return our fake.
        monkeypatch.setattr(
            "services.video_service.get_teslacam_path",
            lambda: str(teslacam),
        )

        db = tmp_path / "cloud_sync.db"
        # Seed a canonical synced row for the same clip.
        canonical = canonical_cloud_path(str(clip))
        assert canonical.startswith("SentryClips/event_q/")

        # Use _init_cloud_tables so the schema is bootstrapped properly.
        monkeypatch.setattr(
            svc, "CLOUD_ARCHIVE_DB_PATH", str(db),
        )
        conn = svc._init_cloud_tables(str(db))
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES (?, 'synced', 0)",
            (canonical,),
        )
        conn.commit()
        conn.close()

        ok, msg = svc.queue_event_for_sync("SentryClips", "event_q")
        assert ok

        # The synced row should still be the only row, with status synced.
        conn2 = sqlite3.connect(str(db))
        try:
            rows = conn2.execute(
                "SELECT file_path, status FROM cloud_synced_files"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == canonical
            assert rows[0][1] == "synced"
        finally:
            conn2.close()

    def test_inserts_canonical_form_not_absolute(self, tmp_path, monkeypatch):
        teslacam = tmp_path / "TeslaCam"
        sentry = teslacam / "SentryClips" / "event_r"
        sentry.mkdir(parents=True)
        clip = sentry / "event_r-front.mp4"
        clip.write_bytes(b"x" * 100)

        monkeypatch.setattr(
            "services.video_service.get_teslacam_path",
            lambda: str(teslacam),
        )
        db = tmp_path / "cloud_sync.db"
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", str(db))

        ok, msg = svc.queue_event_for_sync("SentryClips", "event_r")
        assert ok

        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT file_path, status FROM cloud_synced_files"
            ).fetchall()
            assert len(rows) == 1
            # Critical: must NOT contain the tmp_path absolute prefix.
            assert not rows[0][0].startswith(str(tmp_path))
            assert not rows[0][0].startswith("/")
            # Must start with the canonical SentryClips/ root.
            assert rows[0][0].startswith("SentryClips/event_r/")
            assert rows[0][1] == "queued"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# remove_from_queue: canonical lookup
# ---------------------------------------------------------------------------


class TestRemoveFromQueueCanonical:
    """``remove_from_queue`` canonicalizes its input so callers passing
    legacy absolute paths still match canonical rows."""

    def test_remove_with_absolute_path_matches_canonical_row(
        self, tmp_path, monkeypatch
    ):
        db = tmp_path / "cloud_sync.db"
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", str(db))
        conn = svc._init_cloud_tables(str(db))
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES (?, 'pending', 0)",
            ("SentryClips/event_s",),
        )
        conn.commit()
        conn.close()

        ok, msg = svc.remove_from_queue(
            "/mnt/gadget/part1-ro/TeslaCam/SentryClips/event_s"
        )
        assert ok
        assert msg == "Removed from queue"

        conn2 = sqlite3.connect(str(db))
        try:
            rows = conn2.execute(
                "SELECT COUNT(*) FROM cloud_synced_files"
            ).fetchone()
            assert rows[0] == 0
        finally:
            conn2.close()

    def test_remove_with_canonical_path_works(self, tmp_path, monkeypatch):
        db = tmp_path / "cloud_sync.db"
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", str(db))
        conn = svc._init_cloud_tables(str(db))
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES (?, 'failed', 3)",
            ("ArchivedClips/clip.mp4",),
        )
        conn.commit()
        conn.close()

        ok, msg = svc.remove_from_queue("ArchivedClips/clip.mp4")
        assert ok and msg == "Removed from queue"

    def test_remove_preserves_synced_rows(self, tmp_path, monkeypatch):
        # The protection that synced rows are NOT deleted by
        # remove_from_queue must still work after canonicalization.
        db = tmp_path / "cloud_sync.db"
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", str(db))
        conn = svc._init_cloud_tables(str(db))
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES (?, 'synced', 0)",
            ("ArchivedClips/clip.mp4",),
        )
        conn.commit()
        conn.close()

        ok, msg = svc.remove_from_queue("ArchivedClips/clip.mp4")
        assert ok and msg == "Not in queue"

        conn2 = sqlite3.connect(str(db))
        try:
            rows = conn2.execute(
                "SELECT COUNT(*) FROM cloud_synced_files"
            ).fetchone()
            assert rows[0] == 1
        finally:
            conn2.close()


# ---------------------------------------------------------------------------
# Schema version bump and migration wiring
# ---------------------------------------------------------------------------


class TestSchemaVersionBump:
    def test_schema_version_is_at_least_3(self):
        # v3 added previous_last_error; v4 (issue #202) drops the
        # orphaned live_event_queue table. Test verifies the floor —
        # later versions must still satisfy the canonicalization +
        # previous_last_error contracts these tests cover.
        assert svc._CLOUD_SCHEMA_VERSION >= 3

    def test_init_cloud_tables_runs_migration_on_v1_db(self, tmp_path):
        # Build a DB at v1 with mixed-form rows, then call
        # _init_cloud_tables and verify the rows are canonical and the
        # version is bumped to the current schema (v3 includes both the
        # v2 path canonicalization and the v3 previous_last_error
        # column; v4 additionally drops the orphaned live_event_queue
        # table — but this test's pre-state has no LES table so v4 is
        # a no-op).
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("/home/pi/ArchivedClips/clip.mp4", "pending", 0),
            ("ArchivedClips/already.mp4", "synced", 0),
        ])
        # Mark as v1 in module_versions so _init_cloud_tables sees a
        # legacy DB.
        conn.execute(
            "CREATE TABLE module_versions "
            "(module TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO module_versions (module, version, updated_at) "
            "VALUES ('cloud_archive', 1, '2026-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

        new_conn = svc._init_cloud_tables(str(db))
        try:
            ver = new_conn.execute(
                "SELECT version FROM module_versions WHERE module = 'cloud_archive'"
            ).fetchone()[0]
            assert ver == svc._CLOUD_SCHEMA_VERSION
            paths = sorted(
                r[0] for r in new_conn.execute(
                    "SELECT file_path FROM cloud_synced_files"
                )
            )
            assert paths == ["ArchivedClips/already.mp4", "ArchivedClips/clip.mp4"]
        finally:
            new_conn.close()


class TestMigrationAtomicity:
    """If the migration raises mid-flight, ALL partial rewrites must be
    rolled back AND the version must NOT be bumped, so the migration
    retries on the next process start.

    Pre-fix, ``_init_cloud_tables`` swallowed the exception with
    ``logger.error`` and fell through to the version bump, leaving the
    DB with a few rewritten rows + the rest legacy + version=2 — the
    migration would never run again and the dedup contract would be
    silently broken on every cloud sync from then on.
    """

    def test_exception_rolls_back_all_rewrites(self, tmp_path, monkeypatch):
        # Build a v1 DB with several rows that need rewriting.
        db = tmp_path / "cloud.db"
        conn = _seed_cloud_db(db, [
            ("/home/pi/ArchivedClips/clip_a.mp4", "pending", 0),
            ("/home/pi/ArchivedClips/clip_b.mp4", "pending", 0),
            ("/home/pi/ArchivedClips/clip_c.mp4", "pending", 0),
        ])
        conn.execute(
            "CREATE TABLE module_versions "
            "(module TEXT PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO module_versions (module, version, updated_at) "
            "VALUES ('cloud_archive', 1, '2026-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

        # Patch the migration helper to rewrite one row, then raise.
        original = svc._migrate_canonicalize_paths_v2

        def faulty_migration(c, path):
            # Simulate "first row rewritten OK, then an unexpected
            # error from row 2 onwards" — the exact failure mode the
            # reviewer demonstrated.
            c.row_factory = sqlite3.Row
            c.execute(
                "UPDATE cloud_synced_files SET file_path = ? WHERE id = 1",
                ("ArchivedClips/clip_a.mp4",),
            )
            raise RuntimeError("simulated migration crash")

        monkeypatch.setattr(svc, "_migrate_canonicalize_paths_v2", faulty_migration)

        new_conn = svc._init_cloud_tables(str(db))
        try:
            # Version MUST stay at 1 — migration retries on next start.
            ver = new_conn.execute(
                "SELECT version FROM module_versions WHERE module = 'cloud_archive'"
            ).fetchone()[0]
            assert ver == 1, (
                f"Version was bumped to {ver} despite migration failure — "
                "migration won't retry and dedup is broken"
            )

            # All three rows must still be in their original (legacy)
            # form — the partial rewrite of clip_a was rolled back.
            paths = sorted(
                r[0] for r in new_conn.execute(
                    "SELECT file_path FROM cloud_synced_files"
                )
            )
            assert paths == [
                "/home/pi/ArchivedClips/clip_a.mp4",
                "/home/pi/ArchivedClips/clip_b.mp4",
                "/home/pi/ArchivedClips/clip_c.mp4",
            ], "Partial rewrites were not rolled back"
        finally:
            new_conn.close()

        # Restore the real migration and verify it runs successfully on
        # the next call (proves the retry actually happens).
        monkeypatch.setattr(svc, "_migrate_canonicalize_paths_v2", original)
        # Reset startup_recovery_done so _init_cloud_tables re-runs the
        # version block without the cached side-effect.
        monkeypatch.setattr(svc, "_startup_recovery_done", False)
        retry_conn = svc._init_cloud_tables(str(db))
        try:
            ver = retry_conn.execute(
                "SELECT version FROM module_versions WHERE module = 'cloud_archive'"
            ).fetchone()[0]
            assert ver == svc._CLOUD_SCHEMA_VERSION
            paths = sorted(
                r[0] for r in retry_conn.execute(
                    "SELECT file_path FROM cloud_synced_files"
                )
            )
            assert paths == [
                "ArchivedClips/clip_a.mp4",
                "ArchivedClips/clip_b.mp4",
                "ArchivedClips/clip_c.mp4",
            ]
        finally:
            retry_conn.close()


class TestRemoveFromQueueRejectsTraversal:
    """``remove_from_queue`` is reachable from raw user input via the
    cloud_archive blueprint POST handler. A ``..`` segment must surface
    as a graceful ``(False, "Invalid path")`` result, NOT crash the
    request handler."""

    def test_dotdot_returns_invalid_path(self, tmp_path, monkeypatch):
        db = tmp_path / "cloud_sync.db"
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", str(db))
        # Materialise the schema so the call gets past _init_cloud_tables.
        conn = svc._init_cloud_tables(str(db))
        conn.close()

        ok, msg = svc.remove_from_queue("ArchivedClips/../etc/passwd")
        assert ok is False
        assert msg == "Invalid path"

    def test_dotdot_does_not_delete_anything(self, tmp_path, monkeypatch):
        # Defense in depth: prove the early-return short-circuits any
        # DELETE. Seed an unrelated row and verify it survives.
        db = tmp_path / "cloud_sync.db"
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_DB_PATH", str(db))
        conn = svc._init_cloud_tables(str(db))
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status, retry_count) "
            "VALUES (?, 'pending', 0)",
            ("ArchivedClips/etc",),  # Plausible victim
        )
        conn.commit()
        conn.close()

        ok, msg = svc.remove_from_queue("ArchivedClips/../etc")
        assert ok is False
        assert msg == "Invalid path"

        conn2 = sqlite3.connect(str(db))
        try:
            count = conn2.execute(
                "SELECT COUNT(*) FROM cloud_synced_files"
            ).fetchone()[0]
            assert count == 1, "remove_from_queue deleted a row despite returning Invalid path"
        finally:
            conn2.close()

