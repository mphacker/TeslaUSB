"""Integration test: real ``tesla_gadget_rebind.sh`` argument handling.

Drives the *actual* gadget-rebind helper in ``--dry-run`` so no UDC is
ever touched, exercising the ``--skip-media-reload`` flag end-to-end.
The Python unit tests in ``test_gadget_rebind.py`` mock ``subprocess.run``
and therefore cannot catch a typo in the script's argument parsing or a
regression in the dry-run branch that the recording-liveness watchdog
relies on (recovery must skip the chime re-walk).

The script's prerequisite checks (configfs gadget dir + the hide/present
helpers) run *before* the dry-run branch, so the harness stands up a fake
configfs tree and two executable stub helpers in a tmpdir and points the
script at them via the env overrides the script already honours
(``CONFIGFS_ROOT``, ``HIDE_USB``, ``PRESENT_USB``).

Skipped on platforms without ``bash`` on PATH (notably Windows dev boxes);
it runs in CI / on the Pi instead.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> web/ -> <repo>/scripts/
SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "tesla_gadget_rebind.sh"


pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="shell-script integration test requires POSIX bash; runs in CI / on the Pi",
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


def _stub_helper(path: Path) -> None:
    """Create an executable no-op stub at ``path``."""
    path.write_text("#!/usr/bin/env bash\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_dry_run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the helper in --dry-run against a fake configfs tree."""
    configfs = tmp_path / "configfs"
    gadget_dir = configfs / "g1"
    # The dry-run branch still reads the LUN backing file path; create the
    # full lun.0 dir so the read is a clean miss rather than a path error.
    (gadget_dir / "functions" / "mass_storage.usb0" / "lun.0").mkdir(parents=True)
    hide = tmp_path / "hide-usb"
    present = tmp_path / "present-usb"
    _stub_helper(hide)
    _stub_helper(present)

    env = {
        "CONFIGFS_ROOT": str(configfs),
        "HIDE_USB": str(hide),
        "PRESENT_USB": str(present),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    bash = shutil.which("bash") or "bash"
    return subprocess.run(  # noqa: S603 — argv is fully internal
        [bash, str(SCRIPT), "--dry-run", *args],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
        env=env,
    )


def test_skip_media_reload_dry_run_skips_chime_rewalk(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path, "--skip-media-reload")
    assert result.returncode == 0, result.stderr
    assert "SKIPPING media re-walk" in result.stdout
    # The chime SIGHUP must NOT be part of the planned actions.
    assert "SIGHUP" not in result.stdout


def test_default_dry_run_still_does_chime_rewalk(tmp_path: Path) -> None:
    # Back-compat: without the flag the web-UI chime path is unchanged.
    result = _run_dry_run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "SIGHUP" in result.stdout
    assert "SKIPPING media re-walk" not in result.stdout


def test_unknown_flag_is_a_usage_error(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path, "--no-such-flag")
    assert result.returncode == 2
