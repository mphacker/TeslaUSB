"""Tests for :mod:`teslausb_web.services.gadget_rebind`.

The :class:`GadgetRebinder` shells out to
``/usr/local/bin/tesla_gadget_rebind.sh`` to force a full USB
re-enumeration so Tesla re-reads a changed ``LockChime.wav``. Unlike the
debounced cache invalidator it is synchronous and single-flight.

All tests mock :func:`subprocess.run` — we never spawn the real helper
script. Hardware behaviour is covered by the H-series hardware test.
"""

from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

from teslausb_web.services.gadget_rebind import (
    DEFAULT_COMMAND,
    GadgetRebinder,
    RebindResult,
)


def _completed(returncode: int = 0, stdout: str = "ok", stderr: str = "") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_default_command_is_sudo_n_pinned_script() -> None:
    # The sudoers fragment grants NOPASSWD for exactly this argv; the
    # `-n` makes a misconfigured deploy fail fast instead of hanging.
    assert DEFAULT_COMMAND == (
        "sudo",
        "-n",
        "/usr/local/bin/tesla_gadget_rebind.sh",
    )


def test_rebind_invokes_command_and_reports_success() -> None:
    rebinder = GadgetRebinder(command=("/usr/bin/true",))
    with patch("subprocess.run", return_value=_completed()) as run_mock:
        result = rebinder.rebind()
    run_mock.assert_called_once()
    assert run_mock.call_args.args[0] == ["/usr/bin/true"]
    assert result.ok
    assert result.returncode == 0


def test_rebind_reports_nonzero_without_raising() -> None:
    rebinder = GadgetRebinder(command=("/usr/bin/false",))
    with patch("subprocess.run", return_value=_completed(returncode=2, stderr="wedge")):
        result = rebinder.rebind()
    assert not result.ok
    assert result.returncode == 2
    assert result.stderr == "wedge"


def test_rebind_handles_timeout() -> None:
    rebinder = GadgetRebinder(command=("/usr/bin/true",), timeout_seconds=0.1)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=0.1),
    ):
        result = rebinder.rebind()
    assert not result.ok
    assert "timeout" in result.stderr


def test_rebind_handles_oserror() -> None:
    rebinder = GadgetRebinder(command=("/does/not/exist",))
    with patch("subprocess.run", side_effect=OSError("no such file")):
        result = rebinder.rebind()
    assert not result.ok
    assert "no such file" in result.stderr


def test_rebind_after_shutdown_is_refused() -> None:
    rebinder = GadgetRebinder(command=("/usr/bin/true",))
    rebinder.shutdown()
    with patch("subprocess.run", return_value=_completed()) as run_mock:
        result = rebinder.rebind()
    run_mock.assert_not_called()
    assert not result.ok
    assert "shut down" in result.stderr


def test_rebind_is_single_flight() -> None:
    """A second concurrent caller must not run a second subprocess at the
    same time. The single-flight lock serializes the two shell-outs so
    they never overlap.
    """
    rebinder = GadgetRebinder(command=("/usr/bin/true",))
    in_run = threading.Event()
    release = threading.Event()
    concurrent = {"max": 0, "now": 0}
    counter_lock = threading.Lock()

    def fake_run(*_args: object, **_kwargs: object) -> MagicMock:
        with counter_lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        in_run.set()
        release.wait(timeout=2.0)
        with counter_lock:
            concurrent["now"] -= 1
        return _completed()

    results: list[RebindResult] = []
    results_lock = threading.Lock()

    def worker() -> None:
        res = rebinder.rebind()
        with results_lock:
            results.append(res)

    with patch("subprocess.run", side_effect=fake_run):
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        assert in_run.wait(timeout=2.0)
        t2.start()
        time.sleep(0.05)
        release.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

    assert len(results) == 2
    assert all(r.ok for r in results)
    # The lock guarantees the two shell-outs never overlap.
    assert concurrent["max"] == 1


def test_reload_live_marker_contract_matches_daemon() -> None:
    """The rebind script blocks on a journal token that the teslafat
    daemon logs the instant its SIGHUP re-walk goes live, so the rebind
    only ever re-presents the FRESH chime. The token is a cross-language
    contract: the script's ``RELOAD_LIVE_MARKER`` default MUST equal the
    Rust ``RELOAD_LIVE_MARKER`` constant. Guard against silent drift.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    script = (repo_root / "scripts" / "tesla_gadget_rebind.sh").read_text(encoding="utf-8")
    daemon = (repo_root / "rust" / "crates" / "teslafat" / "src" / "main.rs").read_text(
        encoding="utf-8"
    )

    script_match = re.search(
        r'RELOAD_LIVE_MARKER="\$\{RELOAD_LIVE_MARKER:-([A-Za-z0-9._-]+)\}"', script
    )
    daemon_match = re.search(r'const RELOAD_LIVE_MARKER: &str = "([A-Za-z0-9._-]+)";', daemon)
    assert script_match is not None, "script RELOAD_LIVE_MARKER default not found"
    assert daemon_match is not None, "daemon RELOAD_LIVE_MARKER const not found"
    assert script_match.group(1) == daemon_match.group(1), (
        "RELOAD_LIVE_MARKER drift: script="
        f"{script_match.group(1)!r} daemon={daemon_match.group(1)!r}"
    )
