# ruff: noqa: ANN001  # pytest fixture injection.
"""Tests for StorageHealthService — golden fixtures + severity matrix."""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from teslausb_web.services.storage_health_service import (
    SEV_CRITICAL,
    SEV_OK,
    SEV_UNKNOWN,
    SEV_WARN,
    StorageHealthService,
    StorageHealthServiceConfig,
)


# --------------------------------------------------------------------- helpers


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _build_service(
    *,
    run_results: dict[str, Any] | None = None,
    sysfs_data: dict[str, str] | None = None,
    sudo_prefix: tuple[str, ...] = (),
) -> tuple[StorageHealthService, list[list[str]]]:
    """Build a service whose subprocesses are dispatched by basename.

    ``run_results`` maps the *binary basename* (e.g. ``findmnt``) to
    either a ``subprocess.CompletedProcess``, an ``Exception``, or a
    callable that takes the ``command`` list and returns one of those.
    """
    run_results = run_results or {}
    sysfs_data = sysfs_data or {}
    invocations: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}"

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        invocations.append(list(command))
        # First non-sudo token is the binary.
        binary = next(
            (
                Path(token).name
                for token in command
                if not token.startswith("-") and token not in {"sudo", "-n"}
            ),
            "",
        )
        result = run_results.get(binary)
        if result is None:
            return _completed("")
        if callable(result):
            result = result(command)
        if isinstance(result, BaseException):
            raise result
        return result

    def fake_sysfs(path: Path) -> str:
        if path.name in sysfs_data:
            return sysfs_data[path.name]
        raise FileNotFoundError(str(path))

    cfg = StorageHealthServiceConfig(
        sudo_prefix=sudo_prefix,
        online_check_cache_path=Path(tempfile.gettempdir())
        / f"shs-test-online-{uuid.uuid4().hex}.json",
    )
    service = StorageHealthService(
        cfg,
        which=fake_which,
        run_command=fake_run,
        sysfs_reader=fake_sysfs,
    )
    return service, invocations


# --------------------------------------------------------------------- fixtures


_FINDMNT_OK = "ext4 /dev/mmcblk0p2 rw,noatime,errors=remount-ro\n"
_FINDMNT_RO = "ext4 /dev/mmcblk0p2 ro,noatime\n"
# Excerpt of real tune2fs -l output on a clean ext4 SD card.
_TUNE2FS_CLEAN = """tune2fs 1.47.0 (5-Feb-2023)
Filesystem volume name:   rootfs
Last mount time:          Tue May  6 12:34:11 2025
Last write time:          Wed May  7 09:18:02 2025
Mount count:              48
Maximum mount count:      -1
Check interval:           0 (<none>)
Lifetime writes:          812 GB
Filesystem errors:        0
First error time:         n/a
Last error time:          n/a
"""
_TUNE2FS_CLEAN_WITH_FSCK_DATE = _TUNE2FS_CLEAN + "Last checked:             Sun Apr 14 02:11:42 2024\n"
_TUNE2FS_ERRORED = """Filesystem errors:        3
First error time:         Mon Jan  6 11:12:13 2025
Last error time:          Wed May 14 22:01:09 2025
Mount count:              105
Maximum mount count:      100
Last checked:             Sun Apr 14 02:11:42 2024
"""
_JOURNAL_CLEAN = "May 28 09:00:00 host kernel: nothing here\n"
_JOURNAL_DIRTY = (
    "May 28 09:00:00 host kernel: mmcblk0: error -110 transferring data\n"
    "May 28 09:00:01 host kernel: Buffer I/O error on dev mmcblk0p2\n"
    "May 28 09:00:02 host kernel: EXT4-fs error (device mmcblk0p2): something\n"
    "May 28 09:00:03 host kernel: ordinary log line\n"
)
_SYSTEMCTL_OK = "Sun 2026-05-24 07:50:01 EDT\n"


# --------------------------------------------------------------------- mount


def test_findmnt_rw_parses_options_as_readwrite() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
        sysfs_data={"name": "GF8S5\n", "manfid": "0x00001b\n"},
    )
    snap = service.read_snapshot()
    assert snap.fs_type == "ext4"
    assert snap.device == "/dev/mmcblk0p2"
    assert snap.mount_readonly is False
    assert snap.sd_card_name == "GF8S5"
    assert snap.sd_card_manfid == "0x00001b"


def test_findmnt_ro_triggers_critical() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_RO),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.mount_readonly is True
    assert snap.severity == SEV_CRITICAL
    assert any("read-only" in m for m in snap.messages)


def test_findmnt_missing_binary_records_probe_error() -> None:
    cfg = StorageHealthServiceConfig(
        online_check_cache_path=Path(tempfile.gettempdir())
        / f"shs-test-online-{uuid.uuid4().hex}.json",
    )
    service = StorageHealthService(
        cfg,
        which=lambda _name: None,
        run_command=lambda *_a, **_kw: _completed(""),
        sysfs_reader=lambda _p: (_ for _ in ()).throw(FileNotFoundError()),
    )
    snap = service.read_snapshot()
    assert snap.severity == SEV_UNKNOWN
    assert any("findmnt not found" in e for e in snap.probe_errors)


# --------------------------------------------------------------------- tune2fs


def test_tune2fs_clean_parsed_fields() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN_WITH_FSCK_DATE),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.fs_errors == 0
    assert snap.mount_count == 48
    # tune2fs prints "-1" for disabled max; surfaced as None.
    assert snap.max_mount_count is None
    assert snap.last_checked_iso is not None
    assert snap.last_checked_iso.startswith("2024-04-14T02:11:42")
    # Severity is OK: on a Pi with no initramfs the boot-time fsck
    # timestamp can never advance, so we no longer warn on its age.
    # The new "Read-only filesystem check hasn't run in N days"
    # warning is driven by the online-check cache, which this test
    # does not populate (so no online-check warning either).
    assert snap.severity == SEV_OK


def test_tune2fs_clean_recent_fsck_stays_ok() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.severity == SEV_OK


def test_tune2fs_errored_triggers_critical() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_ERRORED),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.fs_errors == 3
    assert snap.fs_first_error_iso is not None
    assert snap.fs_first_error_iso.startswith("2025-01-06T11:12:13")
    assert snap.fs_last_error_iso is not None
    assert snap.fs_last_error_iso.startswith("2025-05-14T22:01:09")
    assert snap.severity == SEV_CRITICAL


def test_tune2fs_mount_count_exceeded_triggers_warn() -> None:
    out = "Mount count:              30\nMaximum mount count:      20\nFilesystem errors:        0\n"
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(out),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.mount_count == 30
    assert snap.max_mount_count == 20
    assert snap.severity == SEV_WARN


# --------------------------------------------------------------------- journal


def test_journal_counts_kernel_errors_in_24h() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_DIRTY),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    # Three error lines, one ordinary line.
    assert snap.io_errors_24h == 3
    assert snap.severity == SEV_WARN


def test_journal_promotes_to_critical_when_combined_with_fs_errors() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_ERRORED),
            "journalctl": _completed(_JOURNAL_DIRTY),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.severity == SEV_CRITICAL


# --------------------------------------------------------------------- systemctl


def test_systemctl_last_trigger_normalised_to_iso() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.fstrim_last_run_iso is not None
    assert snap.fstrim_last_run_iso.startswith("2026-05-24T07:50:01")
    assert snap.e2scrub_last_run_iso is not None
    assert snap.e2scrub_last_run_iso.startswith("2026-05-24T07:50:01")


def test_systemctl_na_treated_as_none() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed("n/a\n"),
        },
    )
    snap = service.read_snapshot()
    assert snap.fstrim_last_run_iso is None
    assert snap.e2scrub_last_run_iso is None


# --------------------------------------------------------------------- failures


def test_subprocess_timeout_recorded_not_raised() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": subprocess.TimeoutExpired(cmd="findmnt", timeout=4.0),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.fs_type is None
    assert any("findmnt" in e and "timed out" in e for e in snap.probe_errors)


def test_subprocess_oserror_recorded_not_raised() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": OSError("permission denied"),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.fs_errors is None
    assert any("tune2fs" in e for e in snap.probe_errors)


def test_sysfs_read_failure_recorded_per_field() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
        # sysfs_data empty → every read raises FileNotFoundError.
    )
    snap = service.read_snapshot()
    assert snap.sd_card_name is None
    assert snap.sd_card_manfid is None
    # Severity is still OK because the other probes were clean.
    assert snap.severity == SEV_OK


# --------------------------------------------------------------------- sudo


def test_sudo_prefix_prepended_only_to_privileged_invocations() -> None:
    service, invocations = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
        sudo_prefix=("sudo", "-n"),
    )
    service.read_snapshot()
    # findmnt is unprivileged.
    findmnt_call = next(c for c in invocations if "findmnt" in c[0])
    assert findmnt_call[0:2] != ["sudo", "-n"]
    # tune2fs + journalctl are privileged.
    tune_call = next(c for c in invocations if "tune2fs" in " ".join(c))
    assert tune_call[0:2] == ["sudo", "-n"]
    journal_call = next(c for c in invocations if "journalctl" in " ".join(c))
    assert journal_call[0:2] == ["sudo", "-n"]
    # systemctl show on a timer is unprivileged.
    sysctl_call = next(c for c in invocations if "systemctl" in c[0])
    assert sysctl_call[0:2] != ["sudo", "-n"]


# --------------------------------------------------------------------- to_dict


def test_to_dict_round_trips_all_fields() -> None:
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
        sysfs_data={"name": "GF8S5\n", "manfid": "0x00001b\n"},
    )
    snap = service.read_snapshot()
    data = snap.to_dict()
    assert data["severity"] == SEV_OK
    assert data["fs_type"] == "ext4"
    assert data["mount_readonly"] is False
    assert data["sd_card_name"] == "GF8S5"
    assert "messages" in data and isinstance(data["messages"], list)
    assert "probe_errors" in data and isinstance(data["probe_errors"], list)


# --------------------------------------------------------------------- config


def test_config_rejects_empty_sudo_prefix_token() -> None:
    with pytest.raises(ValueError, match="sudo_prefix"):
        StorageHealthServiceConfig(sudo_prefix=("sudo", "  "))


def test_config_rejects_zero_timeout() -> None:
    with pytest.raises(ValueError, match="probe_timeout_seconds"):
        StorageHealthServiceConfig(probe_timeout_seconds=0)


# --------------------------------------------------------------------- fsck-scheduling


def _build_service_with_sentinel(
    sentinel: Path,
    *,
    run_results: dict[str, Any] | None = None,
    sudo_prefix: tuple[str, ...] = (),
) -> tuple[StorageHealthService, list[list[str]]]:
    run_results = run_results or {}
    invocations: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}"

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        invocations.append(list(command))
        binary = next(
            (
                Path(token).name
                for token in command
                if not token.startswith("-") and token not in {"sudo", "-n"}
            ),
            "",
        )
        result = run_results.get(binary)
        if result is None:
            return _completed("")
        if callable(result):
            result = result(command)
        if isinstance(result, BaseException):
            raise result
        return result

    cfg = StorageHealthServiceConfig(
        sudo_prefix=sudo_prefix,
        forcefsck_sentinel=sentinel,
        online_check_cache_path=Path(tempfile.gettempdir())
        / f"shs-test-online-{uuid.uuid4().hex}.json",
    )
    service = StorageHealthService(
        cfg,
        which=fake_which,
        run_command=fake_run,
        sysfs_reader=lambda p: (_ for _ in ()).throw(FileNotFoundError(str(p))),
    )
    return service, invocations


def test_fsck_scheduled_false_when_sentinel_absent(tmp_path: Path) -> None:
    sentinel = tmp_path / "forcefsck"
    service, _ = _build_service_with_sentinel(
        sentinel,
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.fsck_scheduled is False


def test_fsck_scheduled_true_when_sentinel_present(tmp_path: Path) -> None:
    sentinel = tmp_path / "forcefsck"
    sentinel.write_text("")
    service, _ = _build_service_with_sentinel(
        sentinel,
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN_WITH_FSCK_DATE),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.fsck_scheduled is True
    # When a check is already scheduled, the "stale fsck" nag is suppressed
    # — the operator has already chosen the remedy.
    assert all("hasn't been fsck" not in m for m in snap.messages)
    assert any("scheduled" in m.lower() for m in snap.messages)


def test_schedule_fsck_invokes_touch_with_sudo(tmp_path: Path) -> None:
    sentinel = tmp_path / "forcefsck"
    service, invocations = _build_service_with_sentinel(
        sentinel,
        run_results={"touch": _completed("")},
        sudo_prefix=("sudo", "-n"),
    )
    service.schedule_fsck_at_next_boot()
    touch_call = next(c for c in invocations if "touch" in " ".join(c))
    assert touch_call[0:2] == ["sudo", "-n"]
    assert touch_call[-1] == str(sentinel)


def test_schedule_fsck_raises_on_nonzero_return(tmp_path: Path) -> None:
    sentinel = tmp_path / "forcefsck"
    service, _ = _build_service_with_sentinel(
        sentinel,
        run_results={"touch": _completed("", returncode=1, stderr="denied")},
    )
    with pytest.raises(RuntimeError, match="touch"):
        service.schedule_fsck_at_next_boot()


def test_cancel_scheduled_fsck_invokes_rm_with_force_flag(tmp_path: Path) -> None:
    sentinel = tmp_path / "forcefsck"
    service, invocations = _build_service_with_sentinel(
        sentinel,
        run_results={"rm": _completed("")},
        sudo_prefix=("sudo", "-n"),
    )
    service.cancel_scheduled_fsck()
    rm_call = next(c for c in invocations if "rm" in " ".join(c))
    assert rm_call[0:2] == ["sudo", "-n"]
    assert "-f" in rm_call
    assert rm_call[-1] == str(sentinel)


def test_reboot_now_invokes_systemctl_reboot_with_sudo(tmp_path: Path) -> None:
    sentinel = tmp_path / "forcefsck"
    service, invocations = _build_service_with_sentinel(
        sentinel,
        run_results={"systemctl": _completed("")},
        sudo_prefix=("sudo", "-n"),
    )
    service.reboot_now()
    reboot_call = next(c for c in invocations if c[-1] == "reboot")
    assert reboot_call[0:2] == ["sudo", "-n"]
    assert reboot_call[-2].endswith("systemctl")


def test_reboot_now_raises_on_nonzero_return(tmp_path: Path) -> None:
    sentinel = tmp_path / "forcefsck"
    service, _ = _build_service_with_sentinel(
        sentinel,
        run_results={"systemctl": _completed("", returncode=1, stderr="nope")},
    )
    with pytest.raises(RuntimeError, match="systemctl reboot"):
        service.reboot_now()


# --------------------------------------------------------------- cmdline + marker


_CMDLINE_BASE = (
    "console=tty1 root=PARTUUID=abcd-02 rootfstype=ext4 fsck.repair=yes rootwait\n"
)


def _build_cmdline_service(
    tmp_path: Path,
    *,
    cmdline_text: str | None = _CMDLINE_BASE,
    boot_id: str | None = "boot-id-A",
    marker_text: str | None = None,
    run_results: dict[str, Any] | None = None,
    sudo_prefix: tuple[str, ...] = (),
) -> tuple[StorageHealthService, list[list[str]], Path, Path]:
    """Service with a real cmdline.txt, boot-id file, and marker file in tmp_path.

    Privileged writes (touch/rm/install) are intercepted by the fake
    run_command so we can:
      * verify the right command was issued, and
      * actually mutate the underlying files so subsequent probes see
        the new state (real shell sudo isn't available in unit tests).
    """
    cmdline_path = tmp_path / "cmdline.txt"
    if cmdline_text is not None:
        cmdline_path.write_text(cmdline_text)
    boot_id_path = tmp_path / "boot_id"
    if boot_id is not None:
        boot_id_path.write_text(boot_id + "\n")
    marker_path = tmp_path / "fsck-scheduled-boot-id"
    if marker_text is not None:
        marker_path.write_text(marker_text)
    sentinel_path = tmp_path / "forcefsck"

    run_results = dict(run_results or {})
    invocations: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}"

    def fake_install(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        # Strip sudo prefix tokens and parse flags.
        argv = [t for t in cmd if t not in {"sudo", "-n"}]
        # argv[0] is install binary; remainder is install args.
        args = argv[1:]
        if "-d" in args:
            # Create directory.
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            return _completed("")
        # File install: src is second-to-last, dst is last.
        src = Path(args[-2])
        dst = Path(args[-1])
        dst.write_text(src.read_text())
        return _completed("")

    def fake_touch(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        target = Path(cmd[-1])
        target.touch()
        return _completed("")

    def fake_rm(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        target = Path(cmd[-1])
        try:
            target.unlink()
        except FileNotFoundError:
            pass  # rm -f
        return _completed("")

    run_results.setdefault("install", fake_install)
    run_results.setdefault("touch", fake_touch)
    run_results.setdefault("rm", fake_rm)

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        invocations.append(list(cmd))
        binary = next(
            (
                Path(t).name
                for t in cmd
                if not t.startswith("-") and t not in {"sudo", "-n"}
            ),
            "",
        )
        result = run_results.get(binary)
        if result is None:
            return _completed("")
        if callable(result):
            return result(cmd)
        if isinstance(result, BaseException):
            raise result
        return result

    cfg = StorageHealthServiceConfig(
        sudo_prefix=sudo_prefix,
        forcefsck_sentinel=sentinel_path,
        cmdline_paths=(cmdline_path,),
        fsck_marker_path=marker_path,
        boot_id_path=boot_id_path,
        online_check_cache_path=tmp_path / "online-check.json",
    )
    service = StorageHealthService(
        cfg,
        which=fake_which,
        run_command=fake_run,
        sysfs_reader=lambda p: (_ for _ in ()).throw(FileNotFoundError(str(p))),
    )
    return service, invocations, cmdline_path, marker_path


def test_schedule_fsck_adds_cmdline_flag_and_marker(tmp_path: Path) -> None:
    service, invocations, cmdline, marker = _build_cmdline_service(
        tmp_path, boot_id="boot-A"
    )
    service.schedule_fsck_at_next_boot()
    text = cmdline.read_text()
    # Flag appended exactly once; trailing newline preserved.
    assert "fsck.mode=force" in text
    assert text.count("fsck.mode=force") == 1
    assert text.endswith("\n")
    # Marker file written with the current boot id.
    assert marker.read_text().strip() == "boot-A"
    # We invoked install (for cmdline) and at least one install -d (for marker dir).
    install_calls = [c for c in invocations if any("install" in t for t in c)]
    assert len(install_calls) >= 2


def test_schedule_fsck_is_idempotent_when_flag_present(tmp_path: Path) -> None:
    cmdline_with_flag = (
        "console=tty1 root=PARTUUID=abcd-02 fsck.mode=force rootwait\n"
    )
    service, invocations, cmdline, _ = _build_cmdline_service(
        tmp_path, cmdline_text=cmdline_with_flag, boot_id="boot-A"
    )
    service.schedule_fsck_at_next_boot()
    text = cmdline.read_text()
    # Still exactly one occurrence — no doubling.
    assert text.count("fsck.mode=force") == 1


def test_cancel_fsck_strips_cmdline_flag_and_marker(tmp_path: Path) -> None:
    cmdline_with_flag = (
        "console=tty1 root=PARTUUID=abcd-02 fsck.mode=force rootwait\n"
    )
    service, _, cmdline, marker = _build_cmdline_service(
        tmp_path,
        cmdline_text=cmdline_with_flag,
        boot_id="boot-A",
        marker_text="boot-A\n",
    )
    service.cancel_scheduled_fsck()
    text = cmdline.read_text()
    assert "fsck.mode=force" not in text
    # No double spaces left behind.
    assert "  " not in text.strip()
    # Original trailing newline preserved.
    assert text.endswith("\n")
    assert not marker.exists()


def test_fsck_scheduled_true_when_cmdline_has_force(tmp_path: Path) -> None:
    cmdline_with_flag = (
        "console=tty1 root=PARTUUID=abcd-02 fsck.mode=force rootwait\n"
    )
    service, _, _, _ = _build_cmdline_service(
        tmp_path,
        cmdline_text=cmdline_with_flag,
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN_WITH_FSCK_DATE),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    assert snap.fsck_scheduled is True


def test_cleanup_after_fsck_boot_strips_flag_when_boot_id_differs(
    tmp_path: Path,
) -> None:
    cmdline_with_flag = (
        "console=tty1 root=PARTUUID=abcd-02 fsck.mode=force rootwait\n"
    )
    service, _, cmdline, marker = _build_cmdline_service(
        tmp_path,
        cmdline_text=cmdline_with_flag,
        boot_id="boot-B",  # we've rebooted
        marker_text="boot-A\n",  # armed under previous boot
    )
    cleaned = service.cleanup_after_fsck_boot()
    assert cleaned is True
    assert "fsck.mode=force" not in cmdline.read_text()
    assert not marker.exists()


def test_cleanup_after_fsck_boot_is_noop_before_reboot(tmp_path: Path) -> None:
    cmdline_with_flag = (
        "console=tty1 root=PARTUUID=abcd-02 fsck.mode=force rootwait\n"
    )
    service, _, cmdline, marker = _build_cmdline_service(
        tmp_path,
        cmdline_text=cmdline_with_flag,
        boot_id="boot-A",
        marker_text="boot-A\n",  # same boot — operator hasn't rebooted yet
    )
    cleaned = service.cleanup_after_fsck_boot()
    assert cleaned is False
    # Nothing was touched.
    assert "fsck.mode=force" in cmdline.read_text()
    assert marker.exists()


def test_cleanup_after_fsck_boot_is_noop_without_marker(tmp_path: Path) -> None:
    service, _, cmdline, marker = _build_cmdline_service(
        tmp_path, boot_id="boot-A", marker_text=None
    )
    assert service.cleanup_after_fsck_boot() is False
    # cmdline never had the flag in the first place.
    assert "fsck.mode=force" not in cmdline.read_text()


# --------------------------------------------------------------------- online e2fsck


def _build_online_service(
    tmp_path: Path,
    *,
    e2fsck_result: Any = None,
) -> tuple[StorageHealthService, list[list[str]], Path]:
    invocations: list[list[str]] = []
    cache_path = tmp_path / "online-check.json"

    def fake_install(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        argv = [t for t in cmd if t not in {"sudo", "-n"}]
        args = argv[1:]
        if "-d" in args:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return _completed("")
        src = Path(args[-2])
        dst = Path(args[-1])
        dst.write_text(src.read_text())
        return _completed("")

    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        invocations.append(list(cmd))
        binary = next(
            (
                Path(t).name
                for t in cmd
                if not t.startswith("-") and t not in {"sudo", "-n"}
            ),
            "",
        )
        if binary == "install":
            return fake_install(cmd)
        if binary == "e2fsck":
            if isinstance(e2fsck_result, BaseException):
                raise e2fsck_result
            if e2fsck_result is None:
                return _completed("clean\n", returncode=0)
            return e2fsck_result
        if binary == "findmnt":
            return _completed("ext4 /dev/mmcblk0p2 rw,noatime\n")
        return _completed("")

    cfg = StorageHealthServiceConfig(
        online_check_cache_path=cache_path,
    )
    service = StorageHealthService(
        cfg,
        which=lambda name: f"/usr/bin/{name}",
        run_command=fake_run,
        sysfs_reader=lambda p: (_ for _ in ()).throw(FileNotFoundError(str(p))),
    )
    return service, invocations, cache_path


def test_run_online_root_check_rc0_writes_ok_cache(tmp_path: Path) -> None:
    service, invocations, cache = _build_online_service(
        tmp_path,
        e2fsck_result=_completed("Pass 1\nclean", returncode=0),
    )
    record = service.run_online_root_check("/dev/mmcblk0p2")
    assert record["status"] == "ok"
    assert record["return_code"] == 0
    assert cache.exists()
    # The e2fsck command was invoked with -n -f and the device.
    e2fsck_cmds = [c for c in invocations if any("e2fsck" in t for t in c)]
    assert e2fsck_cmds, "e2fsck was not invoked"
    assert "-n" in e2fsck_cmds[0] and "-f" in e2fsck_cmds[0]
    assert "/dev/mmcblk0p2" in e2fsck_cmds[0]


def test_run_online_root_check_rc4_maps_to_warn(tmp_path: Path) -> None:
    service, _, _ = _build_online_service(
        tmp_path,
        e2fsck_result=_completed("Inode 12 has bad block count", returncode=4),
    )
    record = service.run_online_root_check("/dev/mmcblk0p2")
    assert record["status"] == "warn"
    assert record["return_code"] == 4
    assert "false positives" in record["message"]


def test_run_online_root_check_rc8_maps_to_error(tmp_path: Path) -> None:
    service, _, _ = _build_online_service(
        tmp_path,
        e2fsck_result=_completed("device busy", returncode=8, stderr="busy"),
    )
    record = service.run_online_root_check("/dev/mmcblk0p2")
    assert record["status"] == "error"
    assert record["return_code"] == 8


def test_run_online_root_check_timeout_maps_to_error(tmp_path: Path) -> None:
    service, _, _ = _build_online_service(
        tmp_path,
        e2fsck_result=subprocess.TimeoutExpired(cmd="e2fsck", timeout=1),
    )
    record = service.run_online_root_check("/dev/mmcblk0p2")
    assert record["status"] == "error"
    assert "timed out" in record["message"]


def test_maybe_start_background_online_check_skips_when_fresh(tmp_path: Path) -> None:
    service, invocations, cache = _build_online_service(tmp_path)
    # Pre-populate a fresh cache so the freshness gate triggers.
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"status":"ok","timestamp_iso":"2099-01-01T00:00:00+00:00"}')
    started = service.maybe_start_background_online_check("/dev/mmcblk0p2")
    assert started is False
    # No e2fsck invocation should have occurred.
    assert not any("e2fsck" in " ".join(c) for c in invocations)


def test_maybe_start_background_online_check_force_runs_even_when_fresh(
    tmp_path: Path,
) -> None:
    import time as _time

    service, invocations, cache = _build_online_service(tmp_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"status":"ok","timestamp_iso":"2099-01-01T00:00:00+00:00"}')
    started = service.maybe_start_background_online_check(
        "/dev/mmcblk0p2", force=True
    )
    assert started is True
    # Wait briefly for the daemon thread to finish.
    for _ in range(50):
        if not service.is_online_check_running():
            break
        _time.sleep(0.05)
    assert any("e2fsck" in " ".join(c) for c in invocations)


def test_online_check_warn_status_propagates_to_severity(tmp_path: Path) -> None:
    service, _, cache = _build_online_service(
        tmp_path,
        e2fsck_result=_completed("issues", returncode=4),
    )
    service.run_online_root_check("/dev/mmcblk0p2")
    snap = service.read_snapshot()
    assert snap.severity == SEV_WARN
    assert snap.online_check_status == "warn"
    assert snap.online_check_iso is not None


# --------------------------------------------------------------------- local TZ

def test_date_helpers_emit_offset_suffix() -> None:
    """tune2fs + systemctl ISO outputs include a UTC offset so the
    browser can render them in the user's local timezone."""
    service, _ = _build_service(
        run_results={
            "findmnt": _completed(_FINDMNT_OK),
            "tune2fs": _completed(_TUNE2FS_CLEAN_WITH_FSCK_DATE),
            "journalctl": _completed(_JOURNAL_CLEAN),
            "systemctl": _completed(_SYSTEMCTL_OK),
        },
    )
    snap = service.read_snapshot()
    # Either ``+HH:MM`` or ``-HH:MM`` — never a naive timestamp.
    import re as _re

    pattern = _re.compile(r"[+-]\d{2}:\d{2}$")
    assert snap.last_checked_iso is not None
    assert pattern.search(snap.last_checked_iso) is not None
    assert snap.fstrim_last_run_iso is not None
    assert pattern.search(snap.fstrim_last_run_iso) is not None
    assert snap.e2scrub_last_run_iso is not None
    assert pattern.search(snap.e2scrub_last_run_iso) is not None
