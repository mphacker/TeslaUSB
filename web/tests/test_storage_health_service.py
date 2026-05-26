# ruff: noqa: ANN001  # pytest fixture injection.
"""Tests for StorageHealthService — golden fixtures + severity matrix."""

from __future__ import annotations

import subprocess
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

    cfg = StorageHealthServiceConfig(sudo_prefix=sudo_prefix)
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
    cfg = StorageHealthServiceConfig()
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
    # Severity is warn because the fixture's "Last checked" date is
    # > 180 days old — exactly what the heuristic is designed to flag.
    assert snap.severity == SEV_WARN


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
