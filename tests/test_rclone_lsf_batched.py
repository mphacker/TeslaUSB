"""Phase 5.4 — rclone lsf batched listing regression tests.

Pins the contract of ``_list_remote_tree`` and verifies
``_reconcile_with_remote`` issues exactly ONE rclone subprocess call
(rather than the legacy 2 + 1 = 3 calls — one per parent folder).

Legacy implementation: per-parent ``rclone lsf`` loop:

    for folder in CLOUD_ARCHIVE_SYNC_FOLDERS:    # ["SentryClips", "SavedClips"]
        rclone lsf --dirs-only teslausb:remote/<folder>/
    rclone lsf teslausb:remote/ArchivedClips/

Phase 5.4 collapses these into one call:

    rclone lsf --recursive --max-depth=2 teslausb:remote/

…shaving roughly 1 s of subprocess + auth-handshake + network
overhead off every reconcile pass. The new ``_list_remote_tree``
returns a per-parent dict that downstream code consumes; legacy
fallback (``_reconcile_with_remote_legacy``) is preserved for
robustness (used when the batched call fails — e.g. older rclone
version without --recursive support, network blip).

Tests cover:
* parsing of recursive lsf output into per-parent buckets
* directory entries (trailing slash) vs file entries
* failure modes — non-zero rc / timeout / generic exception → None
* single-call invariant — exactly ONE subprocess.run("rclone", "lsf", ...)
* fallback: when batched call returns None, legacy path runs and
  reconciles correctly
* end-to-end: pending → synced when remote dir exists; new INSERT
  when remote file is unknown to DB
"""

from __future__ import annotations

import sqlite3
import subprocess
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from services import cloud_archive_service as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_cloud_db(tmp_path) -> sqlite3.Connection:
    db_path = str(tmp_path / "cloud_sync.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(svc._CLOUD_TABLES_SQL)
    return conn


def _seed(conn, rows):
    """rows: (file_path, status)"""
    for fp, st in rows:
        conn.execute(
            "INSERT INTO cloud_synced_files (file_path, status) VALUES (?, ?)",
            (fp, st),
        )
    conn.commit()


def _completed(stdout: str, returncode: int = 0):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = ""
    cp.returncode = returncode
    return cp


@pytest.fixture
def cloud_conn(tmp_path):
    conn = _make_cloud_db(tmp_path)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# _list_remote_tree — unit
# ---------------------------------------------------------------------------

class TestListRemoteTree:

    def test_parses_recursive_output_into_per_parent_buckets(self):
        # Synthetic rclone lsf --recursive --max-depth=2 output. Event
        # dirs end in / (it's how rclone lsf marks dirs); ArchivedClips
        # files are bare filenames.
        rclone_out = (
            "SentryClips/2026-05-12_10-00-00/\n"
            "SentryClips/2026-05-12_11-00-00/\n"
            "SavedClips/2026-05-12_12-00-00/\n"
            "ArchivedClips/2026-05-12_10-00-00-front.mp4\n"
            "ArchivedClips/2026-05-12_10-00-00-back.mp4\n"
        )
        with patch.object(subprocess, "run",
                          return_value=_completed(rclone_out)) as mock_run:
            tree = svc._list_remote_tree(
                "/tmp/conf", "tesla/dashcam", [],
            )
        assert tree is not None
        # Exactly ONE rclone call:
        assert mock_run.call_count == 1
        args, _kw = mock_run.call_args
        cmd = args[0]
        assert cmd[0] == "rclone" and cmd[1] == "lsf"
        assert "--recursive" in cmd
        assert "--max-depth" in cmd
        # Parent buckets:
        assert tree["SentryClips"] == {
            "2026-05-12_10-00-00/",
            "2026-05-12_11-00-00/",
        }
        assert tree["SavedClips"] == {"2026-05-12_12-00-00/"}
        assert tree["ArchivedClips"] == {
            "2026-05-12_10-00-00-front.mp4",
            "2026-05-12_10-00-00-back.mp4",
        }

    def test_empty_remote_returns_empty_buckets(self):
        with patch.object(subprocess, "run", return_value=_completed("")):
            tree = svc._list_remote_tree("/tmp/conf", "tesla/dashcam", [])
        assert tree is not None
        for parent in (list(svc.CLOUD_ARCHIVE_SYNC_FOLDERS) + ["ArchivedClips"]):
            assert tree[parent] == set()

    def test_unknown_parent_in_output_is_ignored(self):
        # A future remote layout might include other dirs we don't sync;
        # the function must not crash and must drop them silently.
        rclone_out = (
            "SentryClips/2026-05-12_10-00-00/\n"
            "RandomFolder/something/\n"
            "Unrelated/file.txt\n"
        )
        with patch.object(subprocess, "run",
                          return_value=_completed(rclone_out)):
            tree = svc._list_remote_tree("/tmp/conf", "tesla/dashcam", [])
        assert tree is not None
        assert tree["SentryClips"] == {"2026-05-12_10-00-00/"}
        # Unknown parents not in the dict
        assert "RandomFolder" not in tree
        assert "Unrelated" not in tree

    def test_nonzero_returncode_returns_none(self):
        with patch.object(subprocess, "run",
                          return_value=_completed("", returncode=1)):
            tree = svc._list_remote_tree("/tmp/conf", "tesla/dashcam", [])
        assert tree is None

    def test_timeout_returns_none(self):
        def _raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=a[0], timeout=120)
        with patch.object(subprocess, "run", side_effect=_raise_timeout):
            tree = svc._list_remote_tree("/tmp/conf", "tesla/dashcam", [])
        assert tree is None

    def test_generic_exception_returns_none(self):
        with patch.object(subprocess, "run", side_effect=OSError("boom")):
            tree = svc._list_remote_tree("/tmp/conf", "tesla/dashcam", [])
        assert tree is None

    def test_blank_lines_and_bare_parent_dirs_are_skipped(self):
        rclone_out = (
            "\n"
            "SentryClips/\n"
            "SentryClips/2026-05-12_10-00-00/\n"
            "  \n"
        )
        with patch.object(subprocess, "run",
                          return_value=_completed(rclone_out)):
            tree = svc._list_remote_tree("/tmp/conf", "tesla/dashcam", [])
        assert tree is not None
        # The bare "SentryClips/" entry has no rest after the split.
        # The actual event dir does land in the bucket.
        assert tree["SentryClips"] == {"2026-05-12_10-00-00/"}


# ---------------------------------------------------------------------------
# _reconcile_with_remote — single-call + end-to-end
# ---------------------------------------------------------------------------

class TestReconcileBatched:

    def test_single_subprocess_call_per_reconcile(self, cloud_conn):
        """The hot path of Phase 5.4: ONE rclone subprocess call per
        reconcile pass, regardless of how many parent folders there are.
        The legacy code issued one per parent (2 + 1 = 3); this guards
        the win."""
        _seed(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "pending"),
        ])
        rclone_out = "SentryClips/2026-05-12_10-00-00/\n"
        with patch.object(subprocess, "run",
                          return_value=_completed(rclone_out)) as mock_run:
            n = svc._reconcile_with_remote(
                cloud_conn, "/tmp/conf", "tesla/dashcam", [],
            )
        assert mock_run.call_count == 1, (
            f"Expected exactly 1 rclone call (Phase 5.4 batched listing), "
            f"got {mock_run.call_count}. The legacy per-parent loop must "
            f"not be reintroduced."
        )
        # And the entry got flipped to synced.
        assert n == 1
        row = cloud_conn.execute(
            "SELECT status FROM cloud_synced_files WHERE file_path = ?",
            ("SentryClips/2026-05-12_10-00-00",),
        ).fetchone()
        assert row["status"] == "synced"

    def test_pending_event_dir_marked_synced(self, cloud_conn):
        _seed(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "pending"),
            ("SavedClips/2026-05-12_11-00-00", "failed"),
        ])
        rclone_out = (
            "SentryClips/2026-05-12_10-00-00/\n"
            "SavedClips/2026-05-12_11-00-00/\n"
        )
        with patch.object(subprocess, "run",
                          return_value=_completed(rclone_out)):
            n = svc._reconcile_with_remote(
                cloud_conn, "/tmp/conf", "tesla/dashcam", [],
            )
        assert n == 2
        statuses = {
            r["file_path"]: r["status"]
            for r in cloud_conn.execute(
                "SELECT file_path, status FROM cloud_synced_files"
            ).fetchall()
        }
        assert statuses["SentryClips/2026-05-12_10-00-00"] == "synced"
        assert statuses["SavedClips/2026-05-12_11-00-00"] == "synced"

    def test_unknown_remote_file_inserted_as_synced(self, cloud_conn):
        # Pre-tracking upload — file is on remote but not in our DB.
        rclone_out = "ArchivedClips/2026-05-12_10-00-00-front.mp4\n"
        with patch.object(subprocess, "run",
                          return_value=_completed(rclone_out)):
            n = svc._reconcile_with_remote(
                cloud_conn, "/tmp/conf", "tesla/dashcam", [],
            )
        assert n == 1
        row = cloud_conn.execute(
            "SELECT status FROM cloud_synced_files WHERE file_path = ?",
            ("ArchivedClips/2026-05-12_10-00-00-front.mp4",),
        ).fetchone()
        assert row["status"] == "synced"

    def test_already_synced_entry_not_double_counted(self, cloud_conn):
        # Defends against a bug where a reconcile pass re-counts already-
        # synced rows. The UPDATE has WHERE status IN ('pending', 'failed')
        # so synced rows are skipped — but a future refactor could break
        # that.
        _seed(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "synced"),
        ])
        rclone_out = "SentryClips/2026-05-12_10-00-00/\n"
        with patch.object(subprocess, "run",
                          return_value=_completed(rclone_out)):
            n = svc._reconcile_with_remote(
                cloud_conn, "/tmp/conf", "tesla/dashcam", [],
            )
        # 0 reconciled — already synced and no INSERT (the SELECT fires
        # before INSERT to check existence).
        assert n == 0

    def test_falls_back_to_legacy_when_batched_call_fails(self, cloud_conn):
        """When ``_list_remote_tree`` returns None (rclone failure), the
        legacy per-parent path MUST run so reconcile still works."""
        _seed(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "pending"),
        ])

        # First rclone call (the batched --recursive one) returns rc=1
        # → _list_remote_tree returns None → fallback path runs.
        # The fallback path issues one call per parent (3 total) — those
        # all return successful event-dir listings.
        call_outputs = [
            _completed("", returncode=1),               # batched call → fail
            _completed("2026-05-12_10-00-00/\n"),       # SentryClips
            _completed(""),                             # SavedClips
            _completed(""),                             # ArchivedClips
        ]
        with patch.object(subprocess, "run", side_effect=call_outputs):
            n = svc._reconcile_with_remote(
                cloud_conn, "/tmp/conf", "tesla/dashcam", [],
            )
        # Reconcile happened via legacy.
        assert n == 1
        row = cloud_conn.execute(
            "SELECT status FROM cloud_synced_files WHERE file_path = ?",
            ("SentryClips/2026-05-12_10-00-00",),
        ).fetchone()
        assert row["status"] == "synced"


# ---------------------------------------------------------------------------
# Tripwire: NO per-parent lsf calls in the batched happy path
# ---------------------------------------------------------------------------

class TestNoPerParentLsfRegression:

    def test_batched_path_never_calls_per_parent_lsf(self, cloud_conn):
        """A future refactor MUST NOT add per-parent rclone lsf calls
        back into the batched happy path. Trace every subprocess.run
        call and assert exactly one (the --recursive batched listing).
        """
        _seed(cloud_conn, [
            ("SentryClips/2026-05-12_10-00-00", "pending"),
            ("SavedClips/2026-05-12_11-00-00", "pending"),
            ("ArchivedClips/2026-05-12_10-00-00-front.mp4", "pending"),
        ])
        rclone_out = (
            "SentryClips/2026-05-12_10-00-00/\n"
            "SavedClips/2026-05-12_11-00-00/\n"
            "ArchivedClips/2026-05-12_10-00-00-front.mp4\n"
        )
        calls: List[List[str]] = []
        original_run = subprocess.run

        def tracing_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return _completed(rclone_out)

        with patch.object(subprocess, "run", side_effect=tracing_run):
            svc._reconcile_with_remote(
                cloud_conn, "/tmp/conf", "tesla/dashcam", [],
            )
        # Exactly one call.
        assert len(calls) == 1, (
            f"Expected 1 rclone call in batched happy path, got "
            f"{len(calls)}. Calls: {calls}"
        )
        # And it's the recursive one (not a per-parent --dirs-only).
        cmd = calls[0]
        assert "--recursive" in cmd, (
            f"Per-parent --dirs-only call detected — Phase 5.4 batched "
            f"listing has been bypassed. Cmd: {cmd}"
        )
