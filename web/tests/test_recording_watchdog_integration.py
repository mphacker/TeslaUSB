"""Integration tests for the recording-liveness watchdog.

Drives the *actual* ``deploy/recording-stability/recording-watchdog.sh``
in a fully faked environment (fake ``journalctl`` / ``systemctl`` /
``logger`` / re-present primitive, plus fixture sysfs/configfs files) so
the safety-critical detection, rate-limit, cursor, and actuation logic is
exercised end-to-end without ever touching a real UDC.

The script is invoked as root in production, but every privileged read /
write is redirected to a tmpdir via the ``RW_*`` env overrides the script
honours, so these tests run unprivileged.

Skipped on platforms without ``bash`` (notably Windows dev boxes); they
run in CI / on the Pi.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> web/ -> <repo>/deploy/recording-stability/recording-watchdog.sh
SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "deploy"
    / "recording-stability"
    / "recording-watchdog.sh"
)

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="shell-script integration test requires POSIX bash; runs in CI / on the Pi",
    ),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH"),
    pytest.mark.skipif(not SCRIPT.is_file(), reason=f"script not found at {SCRIPT}"),
]

# A /sys/block/nbd0/stat line has 17 whitespace fields; field 7 is
# sectors-written. Build one with a chosen write-sectors value.
def _nbd_stat(write_sectors: int) -> str:
    fields = ["100", "0", "8000", "5", "200", "0", str(write_sectors)] + ["0"] * 10
    return " ".join(fields) + "\n"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _Env:
    """Builds the faked environment and runs the watchdog once."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.state_dir = tmp / "state"
        self.state_dir.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.rebind_marker = tmp / "rebind.called"
        self.scan_lines = tmp / "scan_lines.txt"
        self.scan_lines.write_text("")  # default: no kernel messages

        # --- fixture files ---
        self.boot_id_file = tmp / "boot_id"
        self.uptime_file = tmp / "uptime"
        self.nbd_stat_file = tmp / "nbd_stat"
        self.nbd_size_file = tmp / "nbd_size"
        self.udc_state_file = tmp / "udc_state"
        configfs = tmp / "configfs"
        gdir = configfs / "g1"
        (gdir / "functions" / "mass_storage.usb0" / "lun.0").mkdir(parents=True)
        self.configfs = configfs
        self.udc_file = gdir / "UDC"
        self.lun_file = gdir / "functions" / "mass_storage.usb0" / "lun.0" / "file"

        # --- stubs ---
        self.journalctl = self.bin / "journalctl"
        _stub(
            self.journalctl,
            'args="$*"\n'
            'if [[ "$args" == *"-n0"* ]]; then\n'
            '  echo "-- cursor: ${FAKE_TAIL_CURSOR:-tail0}"; exit 0\n'
            "fi\n"
            'if [[ "$args" == *"--after-cursor"* ]]; then\n'
            '  [[ -f "${FAKE_SCAN_LINES:-/nonexistent}" ]] && cat "${FAKE_SCAN_LINES}"\n'
            '  rc="${FAKE_SCAN_RC:-0}"\n'
            '  if [[ "$rc" == "0" ]]; then echo "-- cursor: ${FAKE_SCAN_CURSOR:-scan1}"; fi\n'
            '  exit "$rc"\n'
            "fi\n"
            "exit 0\n",
        )
        self.systemctl = self.bin / "systemctl"
        _stub(
            self.systemctl,
            'if [[ "$*" == *"is-active"* ]]; then exit "${FAKE_TESLAFAT_RC:-0}"; fi\nexit 0\n',
        )
        self.logger = self.bin / "logger"
        _stub(self.logger, "exit 0\n")
        self.rebind = self.bin / "rebind"
        _stub(self.rebind, 'echo "$@" >> "$REBIND_MARKER"\nexit "${REBIND_RC:-0}"\n')

        # Optional: exercise real /sys/class/udc/*/state symlink discovery
        # (RW_UDC_STATE_FILE unset) instead of the direct-file override.
        self._use_class_dir = False
        self.udc_class_dir = tmp / "udc_class"

    def configure(
        self,
        *,
        boot_id: str = "boot-A",
        uptime: int = 1000,
        udc_state: str = "configured",
        udc_bound: str = "3f980000.usb",
        lun_backing: str = "/dev/nbd0",
        nbd_size: str = "536872960",
        write_sectors: int = 5000,
    ) -> None:
        self.boot_id_file.write_text(boot_id + "\n")
        self.uptime_file.write_text(f"{uptime}.50 90000.0\n")
        self.udc_state_file.write_text(udc_state + "\n")
        self.udc_file.write_text(udc_bound)
        self.lun_file.write_text(lun_backing)
        self.nbd_size_file.write_text(nbd_size + "\n")
        self.nbd_stat_file.write_text(_nbd_stat(write_sectors))

    def write_state(
        self,
        *,
        boot_id: str = "boot-A",
        armed: int = 1,
        prev_udc: str = "configured",
        prev_write: int = 5000,
        udc_bad: int = 0,
        cursor: str = "cur0",
    ) -> None:
        # The script persists udc_state space-stripped (tr -d '[:space:]'),
        # so the 6-field state line never contains an embedded space. Mirror
        # that here or the read -r parse shifts every following field.
        prev_udc = "".join(prev_udc.split())
        (self.state_dir / "recording-watchdog.state").write_text(
            f"{boot_id} {armed} {prev_udc} {prev_write} {udc_bad} {cursor}\n"
        )

    def enable_udc_symlink_discovery(self, state: str) -> None:
        """Lay out /sys/class/udc the way the kernel does: the per-controller
        entry is a SYMLINK into /sys/devices that holds the ``state`` file.
        With RW_UDC_STATE_FILE unset, the script must find it via the glob —
        ``find`` would miss it because it does not descend symlinks."""
        real = self.tmp / "udc_real" / "3f980000.usb"
        real.mkdir(parents=True)
        (real / "state").write_text(state + "\n")
        self.udc_class_dir.mkdir()
        (self.udc_class_dir / "3f980000.usb").symlink_to(real, target_is_directory=True)
        self._use_class_dir = True

    def seed_ledger(self, uptimes: list[int]) -> None:
        (self.state_dir / "recording-watchdog.ledger").write_text(
            "".join(f"{u}\n" for u in uptimes)
        )

    def set_kernel_scan(self, lines: str, *, cursor: str = "scan1", rc: int = 0) -> None:
        self.scan_lines.write_text(lines)
        self._scan_cursor = cursor
        self._scan_rc = rc

    def run(
        self,
        *,
        scan_lines: str = "",
        scan_cursor: str = "scan1",
        scan_rc: int = 0,
        teslafat_rc: int = 0,
        rebind_rc: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        self.scan_lines.write_text(scan_lines)
        env = {
            "PATH": "/usr/bin:/bin",
            "RW_STATE_DIR": str(self.state_dir),
            "RW_JOURNALCTL": str(self.journalctl),
            "RW_SYSTEMCTL": str(self.systemctl),
            "RW_LOGGER": str(self.logger),
            "RW_REBIND_CMD": str(self.rebind),
            "RW_BOOT_ID_FILE": str(self.boot_id_file),
            "RW_UPTIME_FILE": str(self.uptime_file),
            "RW_NBD_STAT_FILE": str(self.nbd_stat_file),
            "RW_NBD_SIZE_FILE": str(self.nbd_size_file),
            "RW_UDC_STATE_FILE": str(self.udc_state_file),
            "RW_CONFIGFS_ROOT": str(self.configfs),
            "REBIND_MARKER": str(self.rebind_marker),
            "REBIND_RC": str(rebind_rc),
            "FAKE_SCAN_LINES": str(self.scan_lines),
            "FAKE_SCAN_CURSOR": scan_cursor,
            "FAKE_SCAN_RC": str(scan_rc),
            "FAKE_TESLAFAT_RC": str(teslafat_rc),
            "FAKE_TAIL_CURSOR": "tailNOW",
        }
        if self._use_class_dir:
            # Exercise real symlink discovery: drop the direct-file override
            # and point the script at the faked /sys/class/udc tree.
            del env["RW_UDC_STATE_FILE"]
            env["RW_UDC_CLASS_DIR"] = str(self.udc_class_dir)
        bash = shutil.which("bash") or "bash"
        return subprocess.run(  # noqa: S603 — argv fully internal
            [bash, str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
            env=env,
        )

    # -- assertions helpers --
    def rebind_called(self) -> bool:
        return self.rebind_marker.is_file() and self.rebind_marker.read_text().strip() != ""

    def rebind_args(self) -> str:
        return self.rebind_marker.read_text() if self.rebind_marker.is_file() else ""

    def degraded(self) -> bool:
        return (self.state_dir / "recording_degraded").is_file()

    def state(self) -> str:
        f = self.state_dir / "recording-watchdog.state"
        return f.read_text() if f.is_file() else ""


@pytest.fixture()
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


# --------------------------------------------------------------------
# Observe-only / arming
# --------------------------------------------------------------------

def test_first_tick_never_acts_even_with_hard_fault(env: _Env) -> None:
    """A fresh boot is observe-only: it must NOT act on the recurring
    early-boot EIO (or any fault) until armed."""
    env.configure(boot_id="boot-A", uptime=1000)  # healthy + past min-uptime
    # no state file -> fresh boot
    r = env.run(scan_lines="nbd0: Other side returned error (5)\n")
    assert r.returncode == 0, r.stderr
    assert not env.rebind_called(), r.stderr
    # It should have armed (healthy + uptime>=90).
    assert env.state().split()[1] == "1", env.state()


def test_observe_only_when_uptime_below_min(env: _Env) -> None:
    env.configure(boot_id="boot-A", uptime=30)
    r = env.run()
    assert r.returncode == 0
    assert not env.rebind_called()
    assert env.state().split()[1] == "0", "must stay un-armed below min uptime"


# --------------------------------------------------------------------
# HARD faults
# --------------------------------------------------------------------

def test_hard_kernel_fault_when_armed_actuates(env: _Env) -> None:
    env.configure()
    env.write_state(cursor="cur0")
    r = env.run(scan_lines="kernel: nbd0: Other side returned error (5)\n")
    assert r.returncode == 0, r.stderr
    assert env.rebind_called(), r.stderr
    assert "--skip-media-reload" in env.rebind_args()


def test_hard_fault_overrides_write_in_flight_guard(env: _Env) -> None:
    """Errored writes can still advance the block counter; a HARD fault
    must actuate regardless of write-sectors advancing."""
    env.configure(write_sectors=9000)
    env.write_state(prev_write=5000, cursor="cur0")  # writes advanced 5000->9000
    r = env.run(scan_lines="blk_update_request: I/O error, dev nbd0, sector 12345\n")
    assert r.returncode == 0, r.stderr
    assert env.rebind_called(), "HARD fault must ignore the write guard"


def test_hard_fault_but_backend_dead_degrades_without_represent(env: _Env) -> None:
    env.configure(nbd_size="0")  # backend structurally dead
    env.write_state(cursor="cur0")
    r = env.run(scan_lines="nbd0: Other side returned error (5)\n")
    assert r.returncode == 0, r.stderr
    assert not env.rebind_called(), "re-present cannot fix a dead backend"
    assert env.degraded()


def test_stale_logs_do_not_actuate(env: _Env) -> None:
    """Armed, but the after-cursor scan returns no new fault lines."""
    env.configure()
    env.write_state(cursor="cur0")
    r = env.run(scan_lines="")  # nothing new since cursor
    assert r.returncode == 0
    assert not env.rebind_called()


def test_invalid_cursor_reseeds_without_acting(env: _Env) -> None:
    env.configure()
    env.write_state(cursor="cur0")
    # journalctl scan fails (cursor vacuumed) -> rc!=0, empty cursor.
    r = env.run(scan_lines="nbd0: Other side returned error (5)\n", scan_rc=1)
    assert r.returncode == 0
    assert not env.rebind_called(), "must not trust a failed kernel scan"


# --------------------------------------------------------------------
# SOFT (UDC drop) faults
# --------------------------------------------------------------------

def test_single_tick_udc_drop_does_not_act(env: _Env) -> None:
    env.configure(udc_state="not attached")
    env.write_state(prev_udc="configured", udc_bad=0, cursor="cur0")
    r = env.run()
    assert r.returncode == 0
    assert not env.rebind_called(), "one tick of UDC-bad must not act (persist>=2)"
    # udc_bad incremented to 1.
    assert env.state().split()[4] == "1", env.state()


def test_persistent_udc_drop_actuates(env: _Env) -> None:
    env.configure(udc_state="not attached")
    env.write_state(prev_udc="not attached", udc_bad=1, cursor="cur0")  # already 1 bad tick
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert env.rebind_called(), "second consecutive UDC-bad tick must act"


def test_udc_suspended_is_healthy(env: _Env) -> None:
    env.configure(udc_state="suspended")
    env.write_state(prev_udc="configured", udc_bad=1, cursor="cur0")
    r = env.run()
    assert r.returncode == 0
    assert not env.rebind_called(), "suspended == car asleep, not a fault"
    assert env.state().split()[4] == "0", "udc_bad must reset on suspended"


def test_udc_drop_blocked_by_write_in_flight(env: _Env) -> None:
    env.configure(udc_state="not attached", write_sectors=9000)
    env.write_state(prev_udc="not attached", prev_write=5000, udc_bad=1, cursor="cur0")
    r = env.run()
    assert r.returncode == 0
    assert not env.rebind_called(), "a healthy write in flight blocks the SOFT path"


# --------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------

def test_rate_limited_within_window(env: _Env) -> None:
    env.configure()
    env.write_state(cursor="cur0")
    env.seed_ledger([900])  # one actuation 100s ago, within the 900s window
    r = env.run(scan_lines="nbd0: Other side returned error (5)\n")
    assert r.returncode == 0
    assert not env.rebind_called(), "1/15min cap must suppress a second actuation"


def test_hard_stop_after_cap(env: _Env) -> None:
    env.configure()
    env.write_state(cursor="cur0")
    # 4 prior actuations this boot, all outside the rolling window so the
    # window cap is not what's tripping — the per-boot HARD-STOP is.
    env.seed_ledger([10, 20, 30, 40])
    r = env.run(scan_lines="nbd0: Other side returned error (5)\n")
    assert r.returncode == 0
    assert not env.rebind_called(), "must stand down after the per-boot hard stop"
    assert env.degraded()


# --------------------------------------------------------------------
# Re-present failure
# --------------------------------------------------------------------

def test_represent_failure_raises_degraded(env: _Env) -> None:
    env.configure()
    env.write_state(cursor="cur0")
    r = env.run(scan_lines="nbd0: Other side returned error (5)\n", rebind_rc=5)
    assert r.returncode == 0
    assert env.rebind_called()
    assert env.degraded(), "a failed re-present must raise DEGRADED"


# --------------------------------------------------------------------
# Regression: media-LUN (nbd1) faults must never actuate
# --------------------------------------------------------------------

def test_nbd1_media_fault_does_not_actuate(env: _Env) -> None:
    """The HARD-fault regex must match ONLY nbd0 (the TeslaCam data LUN).
    A fault on nbd1 (the media/chime LUN) must NOT trigger a full gadget
    re-present that would interrupt a healthy nbd0 recording."""
    env.configure()
    env.write_state(cursor="cur0")
    r = env.run(scan_lines="nbd1: Other side returned error (5)\n")
    assert r.returncode == 0, r.stderr
    assert not env.rebind_called(), "an nbd1 media fault must never re-present"


# --------------------------------------------------------------------
# Regression: real /sys/class/udc symlink discovery (arming)
# --------------------------------------------------------------------

def test_udc_state_discovered_through_symlink(env: _Env) -> None:
    """With RW_UDC_STATE_FILE unset, the script must discover the UDC
    state file through the /sys/class/udc/<controller> SYMLINK (glob),
    not `find` (which does not descend symlinks). If discovery fails,
    udc_state is empty forever and the watchdog NEVER arms."""
    env.configure()  # healthy, uptime past min
    env.enable_udc_symlink_discovery("configured")
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert env.state().split()[1] == "1", (
        "watchdog must arm — UDC state read via symlink glob"
    )


# --------------------------------------------------------------------
# Regression: absent/zero nbd0 size is structural backend death
# --------------------------------------------------------------------

def test_absent_nbd0_size_degrades_without_represent(env: _Env) -> None:
    """A hard fault with an absent (or non-positive) nbd0 size means the
    backend is gone — re-presenting cannot fix it, so it must DEGRADE,
    not actuate."""
    env.configure(nbd_size="-1")  # sentinel for an absent /sys/block/nbd0/size
    env.write_state(cursor="cur0")
    r = env.run(scan_lines="nbd0: Other side returned error (5)\n")
    assert r.returncode == 0, r.stderr
    assert not env.rebind_called(), "re-present cannot fix a missing nbd0"
    assert env.degraded()
