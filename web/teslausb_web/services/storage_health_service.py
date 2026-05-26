"""Probe ext4 / SD-card health for the Settings "Storage Health" card.

Background — what actually threatens the SD card backing the B-1 LUNs
=====================================================================

The B-1 device is a Raspberry Pi with a single SD card formatted ext4.
Tesla's writes land on POSIX files in that ext4 filesystem (via the
`teslafat` daemon's exFAT/FAT32 synthesis). There are **no** real FAT
partitions to fsck; v1's per-partition Filesystem Health Check is
inapplicable.

What can actually go wrong:

* The kernel hits a fatal FS error and remounts ``/`` read-only.
  Tesla's writes then silently fail. **Highest-priority alarm.**
* ext4's persistent error counter (``s_error_count`` in the superblock,
  surfaced by ``dumpe2fs -h``) ticks up after recoverable errors.
  Non-zero = the FS has had trouble.
* Block-layer / mmc driver I/O errors appear in the kernel journal
  (``journalctl -k``). Frequent ones = SD card is dying.
* ``Last checked`` (from ``tune2fs -l``) and ``Mount count`` /
  ``Maximum mount count`` lag tell us whether the weekly
  ``e2scrub_all.timer`` (Debian default) is running.
* SD cards have no SMART / EXT_CSD wear telemetry. The honest answer
  is "replace yearly, keep the cloud archive enabled."

What this service deliberately does **not** do:

* No write-repair. The correct response to corruption is "replace the
  SD card and restore from cloud archive", not click a Repair button.
* No reformat. Operator data is sacred.
* No subprocesses on the hot path. Every probe is wrapped in
  ``try/except`` and falls back to ``None`` on failure so the page
  always renders.

Sudo policy
===========

``dumpe2fs`` and ``tune2fs`` need root to read the ext4 superblock.
The gunicorn worker runs as the unprivileged ``pi`` user, which has
``NOPASSWD: ALL`` via ``/etc/sudoers.d/010_pi-nopasswd`` (the same
policy already relied upon by ``SambaService`` /
``SambaPasswordService``). The ``sudo_prefix`` config tuple — shared
with the Samba services — is prepended to every privileged
invocation. In tests it's empty so command assertions stay readable.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

SEV_OK: Final[str] = "ok"
SEV_WARN: Final[str] = "warn"
SEV_CRITICAL: Final[str] = "critical"
SEV_UNKNOWN: Final[str] = "unknown"

# Sub-second budget per probe so the snapshot completes in ~1 s total
# even if one binary is slow to fork.
_PROBE_TIMEOUT_SECONDS: Final[float] = 4.0

# Free-form thresholds. Keep tunables explicit so reviewers can
# challenge them.
_WARN_DAYS_SINCE_FSCK: Final[int] = 180
_WARN_MOUNT_COUNT_EXCEEDED: Final[bool] = True

# The kernel journal regex matches the messages mmc / block layer
# produces on a failing SD card. The patterns are deliberately broad
# (any ``I/O error``, any ``mmc0`` line that contains ``error``) so a
# new kernel version emitting a slightly different phrasing still
# trips the counter.
_KERNEL_ERROR_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bI/O error\b", re.IGNORECASE),
    re.compile(r"\bend_request\b.*\berror\b", re.IGNORECASE),
    re.compile(r"\bmmc(?:blk)?[0-9]+\b.*\berror\b", re.IGNORECASE),
    re.compile(r"\bBuffer I/O error\b", re.IGNORECASE),
    re.compile(r"\bEXT4-fs error\b", re.IGNORECASE),
)


class _RunTextCommand(Protocol):
    def __call__(
        self,
        command: list[str],
        /,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class StorageHealthServiceConfig:
    """Bindings for the storage-health probes.

    Defaults match the live device (``/`` mounted from
    ``/dev/mmcblk0p2`` with the SD card exposed under
    ``/sys/block/mmcblk0``). Tests override every path so no real
    subprocesses run.
    """

    mount_point: Path = Path("/")
    sd_card_sysfs: Path = Path("/sys/block/mmcblk0/device")
    fstrim_timer_unit: str = "fstrim.timer"
    e2scrub_timer_unit: str = "e2scrub_all.timer"
    binary_findmnt: str = "findmnt"
    binary_dumpe2fs: str = "dumpe2fs"
    binary_tune2fs: str = "tune2fs"
    binary_journalctl: str = "journalctl"
    binary_systemctl: str = "systemctl"
    sudo_prefix: tuple[str, ...] = ()
    probe_timeout_seconds: float = _PROBE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for field_name, value in (
            ("binary_findmnt", self.binary_findmnt),
            ("binary_dumpe2fs", self.binary_dumpe2fs),
            ("binary_tune2fs", self.binary_tune2fs),
            ("binary_journalctl", self.binary_journalctl),
            ("binary_systemctl", self.binary_systemctl),
            ("fstrim_timer_unit", self.fstrim_timer_unit),
            ("e2scrub_timer_unit", self.e2scrub_timer_unit),
        ):
            if not value.strip():
                raise ValueError(f"storage_health: {field_name} must be non-empty")
        for index, prefix_token in enumerate(self.sudo_prefix):
            if not isinstance(prefix_token, str) or not prefix_token.strip():
                raise ValueError(
                    f"storage_health: sudo_prefix[{index}] must be a non-empty string"
                )
        if self.probe_timeout_seconds <= 0:
            raise ValueError("storage_health: probe_timeout_seconds must be > 0")


@dataclass(frozen=True, slots=True)
class StorageHealthSnapshot:
    """One read of the live storage-health signals.

    Every field is optional — a probe that fails sets its field to
    ``None`` so the UI can degrade gracefully. The ``severity`` field
    is the single overall status; ``messages`` is the
    human-orderable list of reasons.
    """

    severity: str
    messages: tuple[str, ...] = ()
    fs_type: str | None = None
    device: str | None = None
    mount_options: str | None = None
    mount_readonly: bool | None = None
    fs_errors: int | None = None
    fs_first_error_iso: str | None = None
    fs_last_error_iso: str | None = None
    last_checked_iso: str | None = None
    mount_count: int | None = None
    max_mount_count: int | None = None
    io_errors_24h: int | None = None
    fstrim_last_run_iso: str | None = None
    e2scrub_last_run_iso: str | None = None
    sd_card_name: str | None = None
    sd_card_manfid: str | None = None
    probe_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "messages": list(self.messages),
            "fs_type": self.fs_type,
            "device": self.device,
            "mount_options": self.mount_options,
            "mount_readonly": self.mount_readonly,
            "fs_errors": self.fs_errors,
            "fs_first_error_iso": self.fs_first_error_iso,
            "fs_last_error_iso": self.fs_last_error_iso,
            "last_checked_iso": self.last_checked_iso,
            "mount_count": self.mount_count,
            "max_mount_count": self.max_mount_count,
            "io_errors_24h": self.io_errors_24h,
            "fstrim_last_run_iso": self.fstrim_last_run_iso,
            "e2scrub_last_run_iso": self.e2scrub_last_run_iso,
            "sd_card_name": self.sd_card_name,
            "sd_card_manfid": self.sd_card_manfid,
            "probe_errors": list(self.probe_errors),
        }


@dataclass
class _MountInfo:
    fs_type: str | None = None
    device: str | None = None
    options: str | None = None


class StorageHealthService:
    """Read-only probe of SD-card / ext4 health.

    Thread-safety: every call to :meth:`read_snapshot` performs a
    fresh probe; there is no shared mutable state. Callers wishing to
    avoid the ~1 s subprocess fan-out per page-load should cache the
    snapshot in ``flask.g``.
    """

    def __init__(
        self,
        config: StorageHealthServiceConfig,
        *,
        which: Callable[[str], str | None] | None = None,
        run_command: _RunTextCommand | None = None,
        sysfs_reader: Callable[[Path], str] | None = None,
    ) -> None:
        config.validate()
        self._config = config
        self._which = shutil.which if which is None else which
        self._run_command = subprocess.run if run_command is None else run_command
        self._sysfs_reader = (
            self._default_sysfs_reader if sysfs_reader is None else sysfs_reader
        )

    @property
    def config(self) -> StorageHealthServiceConfig:
        return self._config

    def read_snapshot(self) -> StorageHealthSnapshot:
        """Run every probe and assemble the snapshot.

        Never raises. Every individual probe is wrapped; failures
        accumulate into :attr:`StorageHealthSnapshot.probe_errors`.
        """
        probe_errors: list[str] = []
        mount = self._probe_mount(probe_errors)
        readonly: bool | None = None
        if mount.options is not None:
            readonly = self._parse_options_readonly(mount.options)
        fs_errors: int | None = None
        fs_first: str | None = None
        fs_last: str | None = None
        last_checked: str | None = None
        mount_count: int | None = None
        max_mount_count: int | None = None
        if mount.device:
            tune_data = self._probe_tune2fs(mount.device, probe_errors)
            fs_errors = tune_data.get("fs_errors")  # type: ignore[assignment]
            fs_first = tune_data.get("first_error_iso")  # type: ignore[assignment]
            fs_last = tune_data.get("last_error_iso")  # type: ignore[assignment]
            last_checked = tune_data.get("last_checked_iso")  # type: ignore[assignment]
            mount_count = tune_data.get("mount_count")  # type: ignore[assignment]
            max_mount_count = tune_data.get("max_mount_count")  # type: ignore[assignment]
        io_errors_24h = self._probe_io_errors_24h(probe_errors)
        fstrim_last = self._probe_timer_last_run(
            self._config.fstrim_timer_unit, probe_errors
        )
        e2scrub_last = self._probe_timer_last_run(
            self._config.e2scrub_timer_unit, probe_errors
        )
        sd_name, sd_manfid = self._probe_sd_card(probe_errors)

        severity, messages = self._derive_severity(
            readonly=readonly,
            fs_errors=fs_errors,
            io_errors_24h=io_errors_24h,
            mount_count=mount_count,
            max_mount_count=max_mount_count,
            last_checked_iso=last_checked,
        )

        return StorageHealthSnapshot(
            severity=severity,
            messages=tuple(messages),
            fs_type=mount.fs_type,
            device=mount.device,
            mount_options=mount.options,
            mount_readonly=readonly,
            fs_errors=fs_errors,
            fs_first_error_iso=fs_first,
            fs_last_error_iso=fs_last,
            last_checked_iso=last_checked,
            mount_count=mount_count,
            max_mount_count=max_mount_count,
            io_errors_24h=io_errors_24h,
            fstrim_last_run_iso=fstrim_last,
            e2scrub_last_run_iso=e2scrub_last,
            sd_card_name=sd_name,
            sd_card_manfid=sd_manfid,
            probe_errors=tuple(probe_errors),
        )

    # ------------------------------------------------------------------
    # Individual probes
    # ------------------------------------------------------------------

    def _probe_mount(self, errors: list[str]) -> _MountInfo:
        """Run ``findmnt -no FSTYPE,SOURCE,OPTIONS <mount>``."""
        binary = self._which(self._config.binary_findmnt)
        if binary is None:
            errors.append("findmnt not found")
            return _MountInfo()
        command = [
            binary,
            "-n",
            "-o",
            "FSTYPE,SOURCE,OPTIONS",
            str(self._config.mount_point),
        ]
        completed = self._run_safe(command, errors, label="findmnt")
        if completed is None or completed.returncode != 0:
            return _MountInfo()
        parts = (completed.stdout or "").strip().split(maxsplit=2)
        if len(parts) < 2:
            errors.append("findmnt: unexpected output shape")
            return _MountInfo()
        fs_type = parts[0]
        device = parts[1]
        options = parts[2] if len(parts) >= 3 else None
        return _MountInfo(fs_type=fs_type, device=device, options=options)

    @staticmethod
    def _parse_options_readonly(options: str) -> bool:
        """Return True if the comma-separated mount-options list has ``ro``.

        ``rw`` is preferred when both appear (the kernel only ever
        prints one of them, but be defensive).
        """
        tokens = {token.strip() for token in options.split(",") if token.strip()}
        if "rw" in tokens:
            return False
        return "ro" in tokens

    def _probe_tune2fs(self, device: str, errors: list[str]) -> dict[str, object]:
        """Parse ``tune2fs -l <device>`` for error / mount counters."""
        binary = self._which(self._config.binary_tune2fs)
        if binary is None:
            errors.append("tune2fs not found")
            return {}
        command = [
            *self._config.sudo_prefix,
            binary,
            "-l",
            device,
        ]
        completed = self._run_safe(command, errors, label="tune2fs")
        if completed is None or completed.returncode != 0:
            return {}
        return self._parse_tune2fs_output(completed.stdout or "")

    @staticmethod
    def _parse_tune2fs_output(text: str) -> dict[str, object]:
        """Extract the fields we care about from ``tune2fs -l`` output.

        Robust against missing keys (the older the FS, the more keys
        are absent). Date fields are normalised to ISO-8601 UTC; if
        the format is unexpected the field is left out.
        """
        out: dict[str, object] = {}
        for raw_line in text.splitlines():
            if ":" not in raw_line:
                continue
            key, _, value = raw_line.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "Filesystem errors":
                out["fs_errors"] = _safe_int(value)
            elif key == "First error time":
                iso = _tune2fs_date_to_iso(value)
                if iso is not None:
                    out["first_error_iso"] = iso
            elif key == "Last error time":
                iso = _tune2fs_date_to_iso(value)
                if iso is not None:
                    out["last_error_iso"] = iso
            elif key == "Last checked":
                iso = _tune2fs_date_to_iso(value)
                if iso is not None:
                    out["last_checked_iso"] = iso
            elif key == "Mount count":
                out["mount_count"] = _safe_int(value)
            elif key == "Maximum mount count":
                parsed = _safe_int(value)
                # tune2fs prints "-1" when disabled; surface as None
                # so the UI doesn't show a confusing "Max: -1".
                if parsed is not None and parsed >= 0:
                    out["max_mount_count"] = parsed
        return out

    def _probe_io_errors_24h(self, errors: list[str]) -> int | None:
        """Count kernel I/O / EXT4 error lines in the last 24 h."""
        binary = self._which(self._config.binary_journalctl)
        if binary is None:
            errors.append("journalctl not found")
            return None
        command = [
            *self._config.sudo_prefix,
            binary,
            "-k",
            "--since",
            "24 hours ago",
            "--no-pager",
            "-o",
            "short",
        ]
        completed = self._run_safe(command, errors, label="journalctl")
        if completed is None or completed.returncode != 0:
            return None
        count = 0
        for line in (completed.stdout or "").splitlines():
            for pattern in _KERNEL_ERROR_PATTERNS:
                if pattern.search(line):
                    count += 1
                    break
        return count

    def _probe_timer_last_run(
        self, unit: str, errors: list[str]
    ) -> str | None:
        """``systemctl show <unit> -p LastTriggerUSec`` → ISO timestamp."""
        binary = self._which(self._config.binary_systemctl)
        if binary is None:
            errors.append("systemctl not found")
            return None
        # systemctl show on a timer unit does NOT need root.
        command = [
            binary,
            "show",
            unit,
            "-p",
            "LastTriggerUSec",
            "--value",
        ]
        completed = self._run_safe(command, errors, label=f"systemctl({unit})")
        if completed is None or completed.returncode != 0:
            return None
        value = (completed.stdout or "").strip()
        if not value or value == "n/a" or value == "0":
            return None
        # Format: "Sun 2026-05-24 07:50:01 EDT" — strip TZ name and
        # treat naively (the wall-clock display is what the operator
        # cares about). The UI prints "X days ago" using the
        # numeric delta.
        return _systemctl_date_to_iso(value)

    def _probe_sd_card(self, errors: list[str]) -> tuple[str | None, str | None]:
        """Read ``name`` + ``manfid`` from the mmc sysfs node."""
        sysfs = self._config.sd_card_sysfs
        name: str | None = None
        manfid: str | None = None
        try:
            name = self._sysfs_reader(sysfs / "name").strip() or None
        except OSError as exc:
            errors.append(f"sysfs name: {exc}")
        try:
            manfid_raw = self._sysfs_reader(sysfs / "manfid").strip()
            manfid = manfid_raw or None
        except OSError as exc:
            errors.append(f"sysfs manfid: {exc}")
        return name, manfid

    @staticmethod
    def _default_sysfs_reader(path: Path) -> str:
        return path.read_text(encoding="ascii", errors="replace")

    # ------------------------------------------------------------------
    # Severity rollup
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_severity(  # noqa: PLR0913 — explicit fields > kwargs bag
        *,
        readonly: bool | None,
        fs_errors: int | None,
        io_errors_24h: int | None,
        mount_count: int | None,
        max_mount_count: int | None,
        last_checked_iso: str | None,
    ) -> tuple[str, list[str]]:
        messages: list[str] = []
        severity = SEV_OK

        if readonly is True:
            severity = SEV_CRITICAL
            messages.append(
                "Filesystem is mounted read-only — Tesla cannot record clips."
            )

        if fs_errors is not None and fs_errors > 0:
            severity = SEV_CRITICAL
            messages.append(
                f"ext4 has logged {fs_errors} filesystem error(s); inspect the journal."
            )

        if io_errors_24h is not None and io_errors_24h > 0:
            # Promote to critical only when paired with one of the
            # above; otherwise "warn" — a transient I/O blip
            # shouldn't scream "replace your SD card".
            if severity == SEV_OK:
                severity = SEV_WARN
            messages.append(
                f"Kernel reported {io_errors_24h} I/O error(s) in the last 24 h."
            )

        if (
            mount_count is not None
            and max_mount_count is not None
            and max_mount_count > 0
            and mount_count > max_mount_count
            and _WARN_MOUNT_COUNT_EXCEEDED
            and severity == SEV_OK
        ):
            severity = SEV_WARN
            messages.append(
                f"Mount count ({mount_count}) has exceeded the configured "
                f"maximum ({max_mount_count}); schedule an offline e2fsck."
            )

        if last_checked_iso is not None and severity == SEV_OK:
            try:
                last_checked = datetime.fromisoformat(last_checked_iso)
                age_days = (
                    datetime.now(timezone.utc) - last_checked.replace(tzinfo=timezone.utc)
                ).days
                if age_days >= _WARN_DAYS_SINCE_FSCK:
                    severity = SEV_WARN
                    messages.append(
                        f"Filesystem hasn't been fsck'd in {age_days} days."
                    )
            except ValueError:
                pass

        # If every probe returned None we can't claim "ok" honestly;
        # callers see severity=unknown and the UI hides the badge.
        if (
            readonly is None
            and fs_errors is None
            and io_errors_24h is None
            and last_checked_iso is None
            and not messages
        ):
            return SEV_UNKNOWN, ["Storage health probes returned no data."]

        if severity == SEV_OK:
            messages.append("All checks passed.")
        return severity, messages

    # ------------------------------------------------------------------
    # Subprocess helper
    # ------------------------------------------------------------------

    def _run_safe(
        self,
        command: list[str],
        errors: list[str],
        *,
        label: str,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._run_command(
                command,
                capture_output=True,
                text=True,
                timeout=self._config.probe_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{label}: timed out")
        except OSError as exc:
            errors.append(f"{label}: {exc}")
        return None


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------


def _safe_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


_TUNE2FS_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<weekday>[A-Za-z]{3})\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<year>\d{4})"
)
_MONTHS: Final[dict[str, int]] = {
    name: index + 1
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    )
}


def _tune2fs_date_to_iso(value: str) -> str | None:
    """Parse e.g. ``Sun Apr 14 02:11:42 2024`` → ``2024-04-14T02:11:42``.

    Returns ``None`` if the value doesn't match (e.g. ``<not set>``).
    """
    value = (value or "").strip()
    if not value or value.startswith("<"):
        return None
    match = _TUNE2FS_DATE_RE.match(value)
    if match is None:
        return None
    month = _MONTHS.get(match.group("mon"))
    if month is None:
        return None
    try:
        day = int(match.group("day"))
        year = int(match.group("year"))
        hour, minute, second = (int(x) for x in match.group("time").split(":"))
        return datetime(year, month, day, hour, minute, second).isoformat()
    except (TypeError, ValueError):
        return None


_SYSTEMCTL_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<weekday>[A-Za-z]{3})\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})"
)


def _systemctl_date_to_iso(value: str) -> str | None:
    """``Sun 2026-05-24 07:50:01 EDT`` → ``2026-05-24T07:50:01``.

    The timezone abbreviation is stripped (we only display the
    wall-clock time, never compute deltas across timezones here).
    """
    match = _SYSTEMCTL_DATE_RE.match((value or "").strip())
    if match is None:
        return None
    return f"{match.group('date')}T{match.group('time')}"


def make_storage_health_service(cfg: WebConfig) -> StorageHealthService:
    """Build the production-defaults service.

    Reuses ``cfg.samba.sudo_prefix`` so the operator only has to
    configure their privileged-command policy in one place
    (``[samba] sudo_prefix``); the storage probes need exactly the
    same NOPASSWD policy.
    """
    return StorageHealthService(
        StorageHealthServiceConfig(sudo_prefix=cfg.samba.sudo_prefix)
    )


__all__ = (
    "SEV_CRITICAL",
    "SEV_OK",
    "SEV_UNKNOWN",
    "SEV_WARN",
    "StorageHealthService",
    "StorageHealthServiceConfig",
    "StorageHealthSnapshot",
    "make_storage_health_service",
)
