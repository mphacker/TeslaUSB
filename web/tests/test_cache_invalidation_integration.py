"""Integration test: real ``tesla_cache_invalidate.sh`` end-to-end.

Drives the *actual* shell script (no mocking) against a tmpdir that
mimics the configfs layout. This is the Phase 4c.5 increment from
``docs/00-PLAN.md`` — the unit tests in ``test_cache_invalidation.py``
mock ``subprocess.run`` so they can't catch e.g. a typo in the
script's CLI parsing, a regression in the exit-code contract, or a
trap that fails to restore on interrupt. This test exercises that
boundary.

Why tmpdir instead of real ``/sys/kernel/config/usb_gadget``:

* Real configfs requires the ``configfs`` + ``libcomposite`` kernel
  modules and root — neither available in the cloud CI we want this
  test to run in. The script supports the ``CONFIGFS_ROOT`` env
  override precisely so this test can point it at a tmpdir.
* The shell script's logic that we want to integration-test is:
  argument parsing, validation, idempotent clear+restore, trap-based
  cleanup, exit codes. None of those require a real kernel-driven
  configfs to exercise.
* The "real kernel gadget" half of the integration story lives in
  Phase H4c (``docs/00-PLAN.md`` H4c.2-H4c.5) where the Pi composes
  a real ``g_mass_storage`` instance and we observe ``dmesg``.

Skipped on platforms without ``bash`` on PATH (notably Windows dev
boxes). Operators on Windows run this test via the ``tools/xbuild``
podman container or wait for CI.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Locate the shell script under test relative to this file.
# tests/ -> web/ -> <repo>/scripts/
SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "tesla_cache_invalidate.sh"


pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "shell-script integration test requires POSIX bash; the "
            "WSL bash on Windows mis-handles native-Windows paths and "
            "this test runs in CI / on the Pi instead"
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


def _make_configfs(tmp_path: Path, *, lun: int = 1, file_value: str = "/dev/nbd1") -> Path:
    """Build the minimum configfs layout the script expects.

    Returns the simulated CONFIGFS_ROOT. The script reads/writes
    ``<root>/g1/functions/mass_storage.usb0/lun.<N>/file`` (the live
    B-1 gadget defaults — see scripts/tesla_cache_invalidate.sh).
    """
    lun_file = _default_lun_file(tmp_path, lun=lun)
    lun_file.parent.mkdir(parents=True)
    lun_file.write_text(file_value)
    return tmp_path


def _default_lun_file(root: Path, *, lun: int = 1) -> Path:
    """Path to the LUN ``file`` attr under the script's default gadget."""
    return root / "g1" / "functions" / "mass_storage.usb0" / f"lun.{lun}" / "file"


def _run_script(
    configfs_root: Path,
    *args: str,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    """Invoke the script with CONFIGFS_ROOT pinned to the tmpdir."""
    env = {
        "CONFIGFS_ROOT": str(configfs_root),
        # Keep PATH minimal but sufficient for `sleep` / `cat`.
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


@pytest.fixture
def configfs(tmp_path: Path) -> Path:
    return _make_configfs(tmp_path)


# ─── Argument-parsing / usage error paths ────────────────────────────


def test_help_flag_exits_zero(tmp_path: Path) -> None:
    res = _run_script(tmp_path, "--help")
    assert res.returncode == 0
    assert "USAGE" in res.stdout
    assert "EXIT CODES" in res.stdout


def test_unknown_flag_exits_two(tmp_path: Path) -> None:
    res = _run_script(tmp_path, "--no-such-flag")
    assert res.returncode == 2
    assert "unknown argument" in res.stderr


def test_lun_non_integer_exits_two(tmp_path: Path) -> None:
    res = _run_script(tmp_path, "--lun", "abc")
    assert res.returncode == 2
    assert "non-negative integer" in res.stderr


def test_eject_ms_non_integer_exits_two(tmp_path: Path) -> None:
    res = _run_script(tmp_path, "--eject-ms", "fast")
    assert res.returncode == 2


def test_lun_missing_value_exits_two(tmp_path: Path) -> None:
    """--lun with no value -> clean exit 2 (NOT bash's exit 1 unbound-variable)."""
    res = _run_script(tmp_path, "--lun")
    assert res.returncode == 2
    assert "requires a value" in res.stderr


def test_gadget_missing_value_exits_two(tmp_path: Path) -> None:
    res = _run_script(tmp_path, "--gadget")
    assert res.returncode == 2
    assert "requires a value" in res.stderr


def test_function_missing_value_exits_two(tmp_path: Path) -> None:
    res = _run_script(tmp_path, "--function")
    assert res.returncode == 2
    assert "requires a value" in res.stderr


def test_eject_ms_missing_value_exits_two(tmp_path: Path) -> None:
    res = _run_script(tmp_path, "--eject-ms")
    assert res.returncode == 2
    assert "requires a value" in res.stderr


def test_missing_gadget_exits_three(tmp_path: Path) -> None:
    # tmp_path has no g1/ subtree — script must detect that
    # the LUN file path doesn't exist and exit 3, NOT 0 or 5.
    res = _run_script(tmp_path, "--lun", "1")
    assert res.returncode == 3
    assert "LUN file not found" in res.stderr


# ─── Happy-path: clear + restore cycle ───────────────────────────────


def test_real_cycle_restores_original_value(configfs: Path) -> None:
    lun_file = _default_lun_file(configfs)
    original = lun_file.read_text()
    assert original == "/dev/nbd1"

    res = _run_script(configfs, "--lun", "1", "--eject-ms", "10")
    assert res.returncode == 0, res.stderr
    # File must end up with the same value as before (clear → restore).
    assert lun_file.read_text() == original


def test_dry_run_does_not_modify_file(configfs: Path) -> None:
    lun_file = _default_lun_file(configfs)
    before = lun_file.read_text()
    res = _run_script(configfs, "--dry-run", "--lun", "1", "--eject-ms", "10")
    assert res.returncode == 0
    assert "DRY-RUN" in res.stdout
    assert lun_file.read_text() == before


def test_idempotent_back_to_back(configfs: Path) -> None:
    lun_file = _default_lun_file(configfs)
    original = lun_file.read_text()
    for _ in range(3):
        res = _run_script(configfs, "--lun", "1", "--eject-ms", "10")
        assert res.returncode == 0, res.stderr
        assert lun_file.read_text() == original


def test_empty_lun_exits_four_without_writing(configfs: Path) -> None:
    lun_file = _default_lun_file(configfs)
    lun_file.write_text("")
    res = _run_script(configfs, "--lun", "1", "--eject-ms", "10")
    assert res.returncode == 4
    # Must still be empty — the script must not invent a value.
    assert lun_file.read_text() == ""


def test_custom_gadget_and_function_names(tmp_path: Path) -> None:
    """The --gadget and --function flags actually redirect the path."""
    lun_dir = tmp_path / "other_gadget" / "functions" / "ms_alt.7" / "lun.3"
    lun_dir.mkdir(parents=True)
    lun_file = lun_dir / "file"
    lun_file.write_text("/dev/nbd3")

    res = _run_script(
        tmp_path,
        "--gadget",
        "other_gadget",
        "--function",
        "ms_alt.7",
        "--lun",
        "3",
        "--eject-ms",
        "10",
    )
    assert res.returncode == 0, res.stderr
    assert lun_file.read_text() == "/dev/nbd3"


# ─── Script hygiene ──────────────────────────────────────────────────


def test_script_is_executable() -> None:
    # The script ships as a real executable in the repo
    # (`git update-index --chmod=+x` persists the bit) so the
    # Phase 6 setup.sh can `install -m 0755` without an extra
    # chmod step, and developers can run the script directly
    # from a checkout. Verify both readable AND executable on
    # POSIX; on Windows the executable bit is synthetic so we
    # check only readable (the integration tests above use
    # `bash <SCRIPT>` rather than direct invocation precisely
    # so they work on either OS).
    assert SCRIPT.is_file()
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IRUSR
    if sys.platform != "win32":
        assert mode & stat.S_IXUSR, (
            f"{SCRIPT} must be executable (run `git update-index --chmod=+x {SCRIPT.name}` to fix)"
        )
