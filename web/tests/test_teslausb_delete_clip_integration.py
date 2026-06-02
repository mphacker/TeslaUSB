"""Integration test: real ``teslausb_delete_clip.sh`` end-to-end.

Drives the *actual* root-owned clip-delete helper (no mocking) against a
tmpdir that mimics the TeslaCam backing tree. The Python unit tests in
``test_privileged_delete.py`` mock ``subprocess.run`` so they cannot catch
a typo in the script's argument parsing, a regression in the containment
validation, or an exit-code contract break. This test exercises that
boundary — and crucially the security-critical path containment checks
that are the whole reason the helper re-validates its untrusted argument.

The helper anchors deletion under ``TESLAUSB_DELETE_ALLOWED_BASE`` (which
production never sets — sudo ``env_reset`` strips it so the default
``/srv/teslausb`` applies). This test points it at a tmpdir.

Skipped on platforms without ``bash`` on PATH (notably Windows dev boxes).
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> web/ -> <repo>/scripts/
SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "teslausb_delete_clip.sh"


pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "shell-script integration test requires POSIX bash; the WSL "
            "bash on Windows mis-handles native-Windows paths and this "
            "test runs in CI / on the Pi instead"
        ),
    ),
    pytest.mark.skipif(
        shutil.which("bash") is None,
        reason="bash not on PATH",
    ),
    pytest.mark.skipif(
        not SCRIPT.is_file(),
        reason=f"script not found at {SCRIPT}",
    ),
]


def _run_script(
    allowed_base: Path,
    *args: str,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    """Invoke the helper with the allowed base pinned to a tmpdir."""
    env = {
        "TESLAUSB_DELETE_ALLOWED_BASE": str(allowed_base),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    bash = shutil.which("bash") or "bash"
    return subprocess.run(  # noqa: S603 — argv is fully internal
        [bash, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _make_event(base: Path, category: str, event: str) -> Path:
    """Create ``base/TeslaCam/<category>/<event>`` with a couple of files."""
    event_dir = base / "TeslaCam" / category / event
    event_dir.mkdir(parents=True)
    (event_dir / "event.json").write_text("{}")
    (event_dir / "2026-06-02_12-00-00-front.mp4").write_bytes(b"\x00\x01")
    return event_dir


# ─── Usage / argument errors ─────────────────────────────────────────


def test_no_args_exits_two(tmp_path: Path) -> None:
    res = _run_script(tmp_path)
    assert res.returncode == 2
    assert "usage" in res.stderr.lower()


def test_too_many_args_exits_two(tmp_path: Path) -> None:
    res = _run_script(tmp_path, "/a", "/b")
    assert res.returncode == 2


def test_relative_path_exits_three(tmp_path: Path) -> None:
    res = _run_script(tmp_path, "TeslaCam/SavedClips/evt")
    assert res.returncode == 3
    assert "absolute" in res.stderr


# ─── Containment rejections (security-critical) ──────────────────────


def test_outside_base_exits_five(tmp_path: Path) -> None:
    base = tmp_path / "srv"
    base.mkdir()
    outside = tmp_path / "etc" / "passwd"
    outside.parent.mkdir()
    outside.write_text("root:x:0:0")
    res = _run_script(base, str(outside))
    assert res.returncode == 5
    assert outside.read_text() == "root:x:0:0"


def test_teslacam_root_itself_rejected(tmp_path: Path) -> None:
    base = tmp_path / "srv"
    teslacam = base / "TeslaCam"
    teslacam.mkdir(parents=True)
    res = _run_script(base, str(teslacam))
    assert res.returncode == 5
    assert teslacam.is_dir()


def test_bare_category_dir_rejected(tmp_path: Path) -> None:
    base = tmp_path / "srv"
    category = base / "TeslaCam" / "SentryClips"
    category.mkdir(parents=True)
    res = _run_script(base, str(category))
    assert res.returncode == 5
    assert category.is_dir()


def test_unknown_category_rejected(tmp_path: Path) -> None:
    base = tmp_path / "srv"
    event = _make_event(base, "NotAClipDir", "2026-06-02_12-00-00")
    res = _run_script(base, str(event))
    assert res.returncode == 5
    assert event.is_dir()


def test_symlinked_target_rejected(tmp_path: Path) -> None:
    base = tmp_path / "srv"
    real_event = _make_event(base, "SavedClips", "2026-06-02_12-00-00")
    link = base / "TeslaCam" / "SavedClips" / "link_event"
    link.symlink_to(real_event)
    res = _run_script(base, str(link))
    assert res.returncode == 6
    assert real_event.is_dir()


# ─── Happy path ──────────────────────────────────────────────────────


def test_deletes_saved_clip_event(tmp_path: Path) -> None:
    base = tmp_path / "srv"
    event = _make_event(base, "SavedClips", "2026-06-02_12-00-00")
    res = _run_script(base, str(event))
    assert res.returncode == 0, res.stderr
    assert not event.exists()


def test_deletes_sentry_clip_event(tmp_path: Path) -> None:
    base = tmp_path / "srv"
    event = _make_event(base, "SentryClips", "2026-05-31_14-56-34")
    res = _run_script(base, str(event))
    assert res.returncode == 0, res.stderr
    assert not event.exists()


def test_deletes_single_file_in_event(tmp_path: Path) -> None:
    base = tmp_path / "srv"
    event = _make_event(base, "RecentClips", "2026-06-02_12-00-00")
    target = event / "2026-06-02_12-00-00-front.mp4"
    res = _run_script(base, str(target))
    assert res.returncode == 0, res.stderr
    assert not target.exists()
    # The event directory itself survives — we deleted only the file.
    assert event.is_dir()


def test_missing_target_is_idempotent(tmp_path: Path) -> None:
    base = tmp_path / "srv"
    (base / "TeslaCam" / "SavedClips").mkdir(parents=True)
    ghost = base / "TeslaCam" / "SavedClips" / "already-gone"
    res = _run_script(base, str(ghost))
    assert res.returncode == 0, res.stderr


def test_nested_base_with_teslacam_segment(tmp_path: Path) -> None:
    """Mirrors the live deploy where backing_root is /srv/teslausb/teslacam."""
    base = tmp_path / "srv" / "teslausb" / "teslacam"
    event = _make_event(base, "SentryClips", "2026-05-31_14-56-34")
    res = _run_script(base, str(event))
    assert res.returncode == 0, res.stderr
    assert not event.exists()


# ─── Script hygiene ──────────────────────────────────────────────────


def test_script_is_executable() -> None:
    assert SCRIPT.is_file()
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IRUSR
    if sys.platform != "win32":
        assert mode & stat.S_IXUSR, (
            f"{SCRIPT} must be executable (run `git update-index --chmod=+x {SCRIPT.name}` to fix)"
        )
