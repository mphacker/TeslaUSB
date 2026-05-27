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

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

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

# Online (read-only) e2fsck cadence. The check is a read-only ``e2fsck
# -nf`` on the live mounted root — safe to run on a mounted RW ext4
# (the ``-n`` answers "no" to every question so nothing is modified)
# but can produce false positives because metadata is in flux. We
# refresh the cache lazily: if the saved result is older than
# ``_ONLINE_CHECK_MAX_AGE_SECONDS`` a background thread is kicked off
# from ``read_snapshot``. The "no online check in N days" severity
# warning trips at ``_WARN_DAYS_SINCE_ONLINE_CHECK`` so a missed
# refresh doesn't silently leave the operator blind.
_ONLINE_CHECK_MAX_AGE_SECONDS: Final[float] = 7 * 24 * 3600.0
_WARN_DAYS_SINCE_ONLINE_CHECK: Final[int] = 14
# e2fsck on a few-GB ext4 on a slow SD card takes 30-90 s in
# practice; we give it a generous ceiling so we don't false-fail.
_ONLINE_CHECK_RUN_TIMEOUT_SECONDS: Final[float] = 600.0

# Online-check status tokens — small, fixed, surfaced to the UI.
ONLINE_CHECK_OK: Final[str] = "ok"  # rc=0: filesystem clean
ONLINE_CHECK_WARN: Final[str] = "warn"  # rc=4: errors reported (read-only run)
ONLINE_CHECK_ERROR: Final[str] = "error"  # rc≥8: e2fsck failed to run
ONLINE_CHECK_UNKNOWN: Final[str] = "unknown"  # never run / cache unreadable

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

# Matches ``fsck.mode=force`` as a standalone whitespace-delimited token in
# the kernel cmdline. We strip the leading whitespace too so collapsing the
# remaining tokens doesn't leave a double space.
_CMDLINE_FORCE_FSCK_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"\s*\bfsck\.mode=force\b"
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
    binary_touch: str = "touch"
    binary_rm: str = "rm"
    binary_install: str = "install"
    binary_e2fsck: str = "e2fsck"
    forcefsck_sentinel: Path = Path("/forcefsck")
    # Kernel cmdline used by U-Boot / Pi firmware. ``/boot/firmware/cmdline.txt``
    # is the modern (post-bookworm) location; ``/boot/cmdline.txt`` is the
    # pre-bookworm path. We probe in order and use the first that exists.
    cmdline_paths: tuple[Path, ...] = (
        Path("/boot/firmware/cmdline.txt"),
        Path("/boot/cmdline.txt"),
    )
    # Persists across web-service restarts but lives on the root FS so the
    # operator's choice to schedule an fsck survives a service crash. We
    # disambiguate "armed but not yet rebooted" vs "rebooted, time to clean
    # up" by storing the boot_id we observed at arm time and comparing
    # against the current boot_id at startup.
    fsck_marker_path: Path = Path("/var/lib/teslausb-web/fsck-scheduled-boot-id")
    boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id")
    # Cached result of the periodic read-only e2fsck. Lives on the
    # root FS so we don't lose history across web-service restarts.
    online_check_cache_path: Path = Path(
        "/var/lib/teslausb-web/online-check.json"
    )
    online_check_max_age_seconds: float = _ONLINE_CHECK_MAX_AGE_SECONDS
    online_check_run_timeout_seconds: float = _ONLINE_CHECK_RUN_TIMEOUT_SECONDS
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
            ("binary_touch", self.binary_touch),
            ("binary_rm", self.binary_rm),
            ("binary_install", self.binary_install),
            ("binary_e2fsck", self.binary_e2fsck),
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
        if self.online_check_max_age_seconds <= 0:
            raise ValueError(
                "storage_health: online_check_max_age_seconds must be > 0"
            )
        if self.online_check_run_timeout_seconds <= 0:
            raise ValueError(
                "storage_health: online_check_run_timeout_seconds must be > 0"
            )


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
    fsck_scheduled: bool = False
    online_check_iso: str | None = None
    online_check_status: str | None = None
    online_check_message: str | None = None
    online_check_running: bool = False
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
            "fsck_scheduled": self.fsck_scheduled,
            "online_check_iso": self.online_check_iso,
            "online_check_status": self.online_check_status,
            "online_check_message": self.online_check_message,
            "online_check_running": self.online_check_running,
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
        # Background-online-check coordination. Single in-process lock
        # — gunicorn typically runs one worker on this Pi, but even if
        # an operator bumps that up, the worst case is two e2fsck
        # processes briefly racing, which is harmless: e2fsck -n makes
        # no writes and the cache file overwrite is atomic via
        # ``install``. Last-write-wins is acceptable here.
        self._online_check_lock = threading.Lock()
        self._online_check_running = False
        self._online_check_thread: threading.Thread | None = None

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
        fsck_scheduled = self._probe_fsck_scheduled(probe_errors)
        online_cache = self._read_online_check_cache(probe_errors)
        online_running = self.is_online_check_running()

        severity, messages = self._derive_severity(
            readonly=readonly,
            fs_errors=fs_errors,
            io_errors_24h=io_errors_24h,
            mount_count=mount_count,
            max_mount_count=max_mount_count,
            last_checked_iso=last_checked,
            fsck_scheduled=fsck_scheduled,
            online_check=online_cache,
            online_check_running=online_running,
        )

        # Lazy background refresh: if the cache is stale (or missing),
        # kick off ``e2fsck -nf`` in a daemon thread. We never block
        # the snapshot path on it. ``maybe_start_background_online_check``
        # is itself a cheap no-op if a run is already in progress or
        # if the cache is fresh.
        if mount.device:
            try:
                self.maybe_start_background_online_check(mount.device)
            except Exception:  # noqa: BLE001 — never block read_snapshot
                logger.exception("storage_health: lazy online-check trigger failed")

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
            fsck_scheduled=fsck_scheduled,
            online_check_iso=(online_cache or {}).get("timestamp_iso"),
            online_check_status=(online_cache or {}).get("status"),
            online_check_message=(online_cache or {}).get("message"),
            online_check_running=online_running,
            probe_errors=tuple(probe_errors),
        )

    # ------------------------------------------------------------------
    # Mutating actions
    # ------------------------------------------------------------------

    def schedule_fsck_at_next_boot(self) -> None:
        """Arm a one-shot root-filesystem fsck for the next boot.

        Three independent things are armed so the feature works on every
        Pi configuration we ship:

        1. ``/forcefsck`` sentinel. ``systemd-fsck@.service`` honours it
           for **non-root** fstab entries (e.g. ``/boot/firmware``) and
           runs ``e2fsck -fy`` / ``dosfsck -a`` before mounting them
           read-write.
        2. ``fsck.mode=force`` token in ``/boot/firmware/cmdline.txt``.
           On the Raspberry Pi there is no initramfs — the kernel mounts
           ``/`` directly from ``root=PARTUUID=…``, so the only thing
           that can force a check of root is a kernel cmdline parameter
           the kernel reads before mount. Without this, scheduling fsck
           is a silent no-op for root.
        3. A boot-id marker (``/var/lib/teslausb-web/fsck-scheduled-boot-id``)
           that captures the current boot_id. :meth:`cleanup_after_fsck_boot`
           uses it to detect "we've rebooted since arming → strip the
           cmdline flag so the *next* boot is fast again".

        Caller still triggers the reboot — we deliberately do NOT
        reboot for them.
        """
        # Touch the sentinel first; if cmdline edit fails we want this
        # to take effect for any non-root mount the operator has added.
        self._touch_forcefsck_sentinel()
        if self._resolve_cmdline_path() is not None:
            self._set_cmdline_force_flag(enabled=True)
            self._write_marker(self._current_boot_id())
        else:
            logger.warning(
                "storage_health: no cmdline.txt found; root-fs fsck cannot "
                "be armed — only non-root fstab entries will be checked."
            )

    def cancel_scheduled_fsck(self) -> None:
        """Disarm the scheduled fsck on every channel we armed."""
        self._remove_forcefsck_sentinel()
        if self._resolve_cmdline_path() is not None:
            self._set_cmdline_force_flag(enabled=False)
        self._remove_marker()

    def cleanup_after_fsck_boot(self) -> bool:
        """Strip ``fsck.mode=force`` if we've rebooted since arming.

        Called once at web-app startup. Idempotent and side-effect-free
        when no fsck has been armed:

        * No marker file → nothing to do.
        * Marker present but ``boot_id`` matches → still pre-reboot;
          leave everything in place.
        * Marker present and ``boot_id`` differs → we've rebooted since
          arming, the fsck has either already run or been skipped, and
          we MUST strip the cmdline flag or every subsequent boot will
          pause for an unnecessary check. Also delete the marker.

        Returns True iff a cleanup was performed.
        """
        marker_id = self._read_marker_boot_id()
        if marker_id is None:
            return False
        current_id = self._current_boot_id()
        if current_id is not None and marker_id == current_id:
            # Operator armed the fsck but hasn't rebooted yet. Leave
            # cmdline and marker in place so the reboot still runs.
            return False
        # We've rebooted (or can't read boot_id — fail closed by
        # cleaning up so we never accidentally pin the slow-boot flag).
        try:
            if self._resolve_cmdline_path() is not None:
                self._set_cmdline_force_flag(enabled=False)
            self._remove_marker()
        except Exception:
            logger.exception(
                "storage_health: failed to clean up fsck arming after reboot"
            )
            return False
        logger.info(
            "storage_health: post-reboot cleanup removed fsck.mode=force from "
            "cmdline.txt and cleared marker"
        )
        return True

    def reboot_now(self) -> None:
        """Reboot the host immediately via ``systemctl reboot``.

        Paired with :meth:`schedule_fsck_at_next_boot` so the operator
        can go from "card is overdue for an fsck" to "fsck is running"
        in one click instead of scheduling and then manually rebooting.

        ``systemctl reboot`` only signals systemd and returns
        immediately; the actual shutdown happens asynchronously after
        we've already returned the HTTP response. Operator-facing
        callers must therefore expect the connection to drop shortly
        after this call succeeds.
        """
        binary = self._which(self._config.binary_systemctl)
        if binary is None:
            raise RuntimeError("systemctl binary not found")
        command = [
            *self._config.sudo_prefix,
            binary,
            "reboot",
        ]
        completed = self._run_command(
            command,
            capture_output=True,
            text=True,
            timeout=self._config.probe_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"systemctl reboot failed (rc={completed.returncode}): "
                f"{(completed.stderr or '').strip()}"
            )

    # ------------------------------------------------------------------
    # Online (read-only) e2fsck
    # ------------------------------------------------------------------

    def is_online_check_running(self) -> bool:
        """Return True if a background ``e2fsck -nf`` is in progress.

        Visible to the UI so the operator's "Check now" button can
        disable itself while a run is underway, instead of queueing a
        second run that the in-process lock would silently no-op.
        """
        with self._online_check_lock:
            return self._online_check_running

    def maybe_start_background_online_check(
        self, device: str | None = None, *, force: bool = False
    ) -> bool:
        """Spawn an ``e2fsck -nf`` run in a daemon thread if appropriate.

        Returns True iff a new run was actually started. No-op when:

        * another run is already in progress (in this process), or
        * the cached result is fresh (younger than
          :attr:`StorageHealthServiceConfig.online_check_max_age_seconds`),
          unless ``force=True``.

        ``device`` defaults to the live root device when omitted.
        Failures to resolve the device are logged and swallowed —
        the lazy refresh must never break the snapshot path.
        """
        with self._online_check_lock:
            if self._online_check_running:
                return False
            if not force and self._online_check_cache_is_fresh():
                return False
            self._online_check_running = True

        target_device = device or self._resolve_root_device()
        if not target_device:
            with self._online_check_lock:
                self._online_check_running = False
            logger.warning(
                "storage_health: cannot start online check — root device unknown"
            )
            return False

        thread = threading.Thread(
            target=self._online_check_worker,
            args=(target_device,),
            name="storage-health-online-check",
            daemon=True,
        )
        with self._online_check_lock:
            self._online_check_thread = thread
        thread.start()
        return True

    def run_online_root_check(self, device: str | None = None) -> dict[str, Any]:
        """Synchronously run ``e2fsck -nf <device>`` and return the cached dict.

        Blocking — intended for tests, the on-demand endpoint's
        background worker, and the startup refresh. Production callers
        on a request thread should use
        :meth:`maybe_start_background_online_check` instead.

        Refuses to start if another run is already underway in this
        process (returns the existing cache unchanged) — prevents two
        concurrent e2fsck processes piling on the same SD card.
        """
        with self._online_check_lock:
            if self._online_check_running:
                cached = self._read_online_check_cache([])
                if cached is not None:
                    return cached
                return self._online_check_record(
                    status=ONLINE_CHECK_UNKNOWN,
                    return_code=None,
                    message="A read-only filesystem check is already running.",
                    output_excerpt="",
                )
            self._online_check_running = True

        try:
            target = device or self._resolve_root_device()
            if not target:
                record = self._online_check_record(
                    status=ONLINE_CHECK_ERROR,
                    return_code=None,
                    message="Could not resolve root device for e2fsck.",
                    output_excerpt="",
                )
                self._write_online_check_cache(record)
                return record
            record = self._invoke_e2fsck(target)
            self._write_online_check_cache(record)
            return record
        finally:
            with self._online_check_lock:
                self._online_check_running = False

    def _online_check_worker(self, device: str) -> None:
        """Thread entry point — never raises out to the runtime."""
        try:
            target = device or self._resolve_root_device()
            if not target:
                record = self._online_check_record(
                    status=ONLINE_CHECK_ERROR,
                    return_code=None,
                    message="Could not resolve root device for e2fsck.",
                    output_excerpt="",
                )
            else:
                record = self._invoke_e2fsck(target)
            self._write_online_check_cache(record)
        except Exception:  # noqa: BLE001 — background thread
            logger.exception("storage_health: online-check worker crashed")
        finally:
            with self._online_check_lock:
                self._online_check_running = False

    def _invoke_e2fsck(self, device: str) -> dict[str, Any]:
        """Run ``e2fsck -nf`` against ``device`` and build a cache record.

        ``-n`` answers "no" to every prompt (no writes), ``-f`` forces
        a full check even if the superblock says "clean". Result codes
        per ``e2fsck(8)``:

          0  -> filesystem clean (status=ok)
          1  -> errors corrected (impossible with -n)
          2  -> system should reboot (also impossible with -n)
          4  -> errors left UNcorrected (we asked it not to fix → warn)
          8  -> operational error (couldn't run; status=error)
         16+ -> usage / library error (status=error)
        """
        binary = self._which(self._config.binary_e2fsck)
        if binary is None:
            return self._online_check_record(
                status=ONLINE_CHECK_ERROR,
                return_code=None,
                message="e2fsck binary not found on PATH.",
                output_excerpt="",
            )
        command = [
            *self._config.sudo_prefix,
            binary,
            "-n",
            "-f",
            device,
        ]
        try:
            completed = self._run_command(
                command,
                capture_output=True,
                text=True,
                timeout=self._config.online_check_run_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._online_check_record(
                status=ONLINE_CHECK_ERROR,
                return_code=None,
                message=(
                    "e2fsck timed out after "
                    f"{int(self._config.online_check_run_timeout_seconds)}s."
                ),
                output_excerpt="",
            )
        except OSError as exc:
            return self._online_check_record(
                status=ONLINE_CHECK_ERROR,
                return_code=None,
                message=f"e2fsck failed to launch: {exc}",
                output_excerpt="",
            )
        rc = completed.returncode
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        excerpt = self._online_check_excerpt(stdout, stderr)
        if rc == 0:
            status = ONLINE_CHECK_OK
            message = "Filesystem clean (read-only check)."
        elif rc == 4:
            status = ONLINE_CHECK_WARN
            message = (
                "Read-only check reported potential issues. "
                "May include false positives because the filesystem is "
                "mounted read-write. Consider an offline check from "
                "external media."
            )
        else:
            status = ONLINE_CHECK_ERROR
            message = f"e2fsck failed (rc={rc})."
        return self._online_check_record(
            status=status,
            return_code=rc,
            message=message,
            output_excerpt=excerpt,
        )

    @staticmethod
    def _online_check_excerpt(stdout: str, stderr: str) -> str:
        """Tail of combined output — enough for the UI to show context.

        We keep it bounded so a chatty e2fsck run can't bloat the
        cache file unboundedly.
        """
        combined = "\n".join(s for s in (stdout, stderr) if s).strip()
        if not combined:
            return ""
        lines = combined.splitlines()
        tail = lines[-20:]
        excerpt = "\n".join(tail)
        max_chars = 4096
        if len(excerpt) > max_chars:
            excerpt = excerpt[-max_chars:]
        return excerpt

    @staticmethod
    def _online_check_record(
        *,
        status: str,
        return_code: int | None,
        message: str,
        output_excerpt: str,
    ) -> dict[str, Any]:
        return {
            "timestamp_iso": _local_iso(datetime.now()),
            "status": status,
            "return_code": return_code,
            "message": message,
            "output_excerpt": output_excerpt,
        }

    def _online_check_cache_is_fresh(self) -> bool:
        """True if the cache file exists and is younger than the TTL."""
        path = self._config.online_check_cache_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        age = max(0.0, time.time() - mtime)
        return age < self._config.online_check_max_age_seconds

    def _read_online_check_cache(
        self, errors: list[str]
    ) -> dict[str, Any] | None:
        """Return the parsed cache file, or None if missing/unreadable."""
        path = self._config.online_check_cache_path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            errors.append(f"online-check cache read: {exc}")
            return None
        try:
            data = json.loads(raw)
        except ValueError as exc:
            errors.append(f"online-check cache parse: {exc}")
            return None
        if not isinstance(data, dict):
            errors.append("online-check cache: not a JSON object")
            return None
        return data

    def _write_online_check_cache(self, record: dict[str, Any]) -> None:
        """Persist ``record`` to the cache path via sudo install (atomic-ish).

        Same staging strategy as ``_write_marker`` — pi-writable tmp
        + sudo install for permissions/atomicity.
        """
        binary = self._which(self._config.binary_install)
        if binary is None:
            logger.error(
                "storage_health: install binary missing; cannot persist "
                "online-check cache"
            )
            return
        payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
        path = self._config.online_check_cache_path
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=tempfile.gettempdir(),
                prefix="teslausb-onlinechk.",
                suffix=".json",
            ) as tf:
                tf.write(payload)
                tf.flush()
                tmp_path = Path(tf.name)
        except OSError as exc:
            logger.error("storage_health: stage online-check cache failed: %s", exc)
            return
        try:
            # Make sure the parent directory exists before installing
            # the cache file — first-run installs may not have
            # /var/lib/teslausb-web yet.
            mkdir = self._run_command(
                [
                    *self._config.sudo_prefix,
                    binary,
                    "-d",
                    "-m",
                    "0755",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    str(path.parent),
                ],
                capture_output=True,
                text=True,
                timeout=self._config.probe_timeout_seconds,
                check=False,
            )
            if mkdir.returncode != 0:
                logger.error(
                    "storage_health: install -d %s failed: %s",
                    path.parent,
                    (mkdir.stderr or "").strip(),
                )
                return
            completed = self._run_command(
                [
                    *self._config.sudo_prefix,
                    binary,
                    "-m",
                    "0644",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    str(tmp_path),
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=self._config.probe_timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                logger.error(
                    "storage_health: install %s → %s failed: %s",
                    tmp_path,
                    path,
                    (completed.stderr or "").strip(),
                )
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _resolve_root_device(self) -> str | None:
        """Return the block device backing the configured mount point."""
        try:
            mount = self._probe_mount([])
            return mount.device
        except Exception:  # noqa: BLE001 — never raise from a probe
            return None

    # ------------------------------------------------------------------
    # Privileged file helpers (forcefsck sentinel, cmdline.txt, marker)
    # ------------------------------------------------------------------

    def _touch_forcefsck_sentinel(self) -> None:
        binary = self._which(self._config.binary_touch)
        if binary is None:
            raise RuntimeError("touch binary not found")
        completed = self._run_command(
            [*self._config.sudo_prefix, binary, str(self._config.forcefsck_sentinel)],
            capture_output=True,
            text=True,
            timeout=self._config.probe_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"touch {self._config.forcefsck_sentinel} failed (rc="
                f"{completed.returncode}): {(completed.stderr or '').strip()}"
            )

    def _remove_forcefsck_sentinel(self) -> None:
        binary = self._which(self._config.binary_rm)
        if binary is None:
            raise RuntimeError("rm binary not found")
        completed = self._run_command(
            [
                *self._config.sudo_prefix,
                binary,
                "-f",
                str(self._config.forcefsck_sentinel),
            ],
            capture_output=True,
            text=True,
            timeout=self._config.probe_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"rm -f {self._config.forcefsck_sentinel} failed (rc="
                f"{completed.returncode}): {(completed.stderr or '').strip()}"
            )

    def _resolve_cmdline_path(self) -> Path | None:
        """Return the first configured cmdline.txt that exists, or None."""
        for candidate in self._config.cmdline_paths:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    def _cmdline_has_force_flag(self) -> bool:
        path = self._resolve_cmdline_path()
        if path is None:
            return False
        try:
            return _CMDLINE_FORCE_FSCK_TOKEN.search(path.read_text()) is not None
        except OSError as exc:
            logger.warning("storage_health: read cmdline %s failed: %s", path, exc)
            return False

    def _set_cmdline_force_flag(self, *, enabled: bool) -> None:
        """Atomically rewrite cmdline.txt to add/strip ``fsck.mode=force``.

        Writes to a tempfile in the destination directory first (so the
        rename is atomic within the same FS) and then invokes
        ``install`` via sudo to put it in place with root ownership and
        the original permission bits. Idempotent — no-op when the file
        already reflects the desired state.
        """
        path = self._resolve_cmdline_path()
        if path is None:
            raise RuntimeError("cmdline.txt not found in any configured path")
        try:
            current = path.read_text()
        except OSError as exc:
            raise RuntimeError(f"read {path} failed: {exc}") from exc
        # cmdline.txt is conventionally a single line; preserve the
        # operator's trailing-newline convention.
        trailing_nl = current.endswith("\n")
        body = current.rstrip("\n")
        stripped = _CMDLINE_FORCE_FSCK_TOKEN.sub("", body)
        # Collapse any double spaces the strip may have introduced.
        stripped = re.sub(r"[ \t]+", " ", stripped).strip()
        if enabled:
            new_body = stripped + " fsck.mode=force"
        else:
            new_body = stripped
        new = new_body + ("\n" if trailing_nl else "")
        if new == current:
            return  # already in desired state
        # Stage the new contents in the system temp dir (pi user lacks
        # write on /boot/firmware), then let sudo install rewrite the
        # destination atomically with respect to readers — install
        # itself opens with O_TRUNC, so a crash mid-write could leave
        # a partial cmdline.txt regardless of where the staging file
        # lives. The trade-off is acceptable for a one-shot operator
        # action; we sacrifice rename-atomicity for the ability to
        # stage as the unprivileged user.
        binary = self._which(self._config.binary_install)
        if binary is None:
            raise RuntimeError("install binary not found")
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=tempfile.gettempdir(),
            prefix="teslausb-cmdline.",
            suffix=".new",
        ) as tf:
            tf.write(new)
            tf.flush()
            tmp_path = Path(tf.name)
        try:
            completed = self._run_command(
                [
                    *self._config.sudo_prefix,
                    binary,
                    "-m",
                    "0755",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    str(tmp_path),
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=self._config.probe_timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"install {tmp_path} → {path} failed (rc="
                    f"{completed.returncode}): {(completed.stderr or '').strip()}"
                )
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _current_boot_id(self) -> str | None:
        try:
            return self._config.boot_id_path.read_text().strip() or None
        except OSError as exc:
            logger.warning("storage_health: read boot_id failed: %s", exc)
            return None

    def _read_marker_boot_id(self) -> str | None:
        path = self._config.fsck_marker_path
        try:
            if not path.is_file():
                return None
            return path.read_text().strip() or None
        except OSError as exc:
            logger.warning("storage_health: read fsck marker failed: %s", exc)
            return None

    def _write_marker(self, boot_id: str | None) -> None:
        """Persist the boot-id under which the fsck was armed."""
        if not boot_id:
            # No boot_id means we can never detect "we rebooted" reliably.
            # Skip marker creation so we don't accidentally strip the
            # cmdline flag before the reboot happens.
            logger.warning(
                "storage_health: boot_id unavailable; cmdline flag will not "
                "be auto-stripped after reboot"
            )
            return
        binary = self._which(self._config.binary_install)
        if binary is None:
            raise RuntimeError("install binary not found")
        path = self._config.fsck_marker_path
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=tempfile.gettempdir(),
            prefix="teslausb-fsck-marker.",
        ) as tf:
            tf.write(boot_id + "\n")
            tf.flush()
            tmp_path = Path(tf.name)
        try:
            # Make sure the parent directory exists (fresh installs may
            # not have /var/lib/teslausb-web yet).
            mkdir = self._run_command(
                [
                    *self._config.sudo_prefix,
                    binary,
                    "-d",
                    "-m",
                    "0755",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    str(path.parent),
                ],
                capture_output=True,
                text=True,
                timeout=self._config.probe_timeout_seconds,
                check=False,
            )
            if mkdir.returncode != 0:
                raise RuntimeError(
                    f"install -d {path.parent} failed (rc={mkdir.returncode}): "
                    f"{(mkdir.stderr or '').strip()}"
                )
            completed = self._run_command(
                [
                    *self._config.sudo_prefix,
                    binary,
                    "-m",
                    "0644",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    str(tmp_path),
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=self._config.probe_timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"install {tmp_path} → {path} failed (rc="
                    f"{completed.returncode}): {(completed.stderr or '').strip()}"
                )
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _remove_marker(self) -> None:
        binary = self._which(self._config.binary_rm)
        if binary is None:
            raise RuntimeError("rm binary not found")
        completed = self._run_command(
            [
                *self._config.sudo_prefix,
                binary,
                "-f",
                str(self._config.fsck_marker_path),
            ],
            capture_output=True,
            text=True,
            timeout=self._config.probe_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"rm -f {self._config.fsck_marker_path} failed (rc="
                f"{completed.returncode}): {(completed.stderr or '').strip()}"
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

    def _probe_fsck_scheduled(self, errors: list[str]) -> bool:
        """Return True iff a one-shot fsck is armed for the next boot.

        We treat *either* armed channel as scheduled so the UI reflects
        reality regardless of which mechanism the operator (or a prior
        version of this code) used:

        * ``/forcefsck`` sentinel exists, OR
        * ``fsck.mode=force`` is present in the kernel cmdline.

        Either alone is enough to make the operator's check actually
        run, so either alone justifies showing "scheduled" in the UI.
        """
        sentinel_present = False
        try:
            sentinel_present = self._config.forcefsck_sentinel.exists()
        except OSError as exc:
            errors.append(f"forcefsck sentinel: {exc}")
        try:
            if self._cmdline_has_force_flag():
                return True
        except Exception as exc:  # noqa: BLE001 — probe must never fail
            errors.append(f"cmdline fsck flag: {exc}")
        return sentinel_present

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
        fsck_scheduled: bool = False,
        online_check: dict[str, Any] | None = None,
        online_check_running: bool = False,
    ) -> tuple[str, list[str]]:
        # If every probe returned None we can't claim anything honestly;
        # callers see severity=unknown and the UI hides the badge.
        if (
            readonly is None
            and fs_errors is None
            and io_errors_24h is None
            and last_checked_iso is None
            and online_check is None
            and not fsck_scheduled
        ):
            return SEV_UNKNOWN, ["Storage health probes returned no data."]

        messages: list[str] = []
        severity = SEV_OK

        if fsck_scheduled:
            messages.append(
                "Filesystem check is scheduled to run at next boot."
            )

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

        # Online (read-only) check — drives a separate severity bump.
        # We DO NOT warn on ``last_checked_iso`` age any more: on a Pi
        # with no initramfs that timestamp can never advance, so it
        # would warn forever no matter what the operator did. The
        # online check advances on every successful background run.
        online_status = (online_check or {}).get("status")
        online_iso = (online_check or {}).get("timestamp_iso")
        online_message = (online_check or {}).get("message")
        if online_status == ONLINE_CHECK_WARN:
            if severity == SEV_OK:
                severity = SEV_WARN
            messages.append(
                online_message
                or "Read-only filesystem check reported potential issues."
            )
        elif online_status == ONLINE_CHECK_ERROR:
            if severity == SEV_OK:
                severity = SEV_WARN
            messages.append(
                online_message or "Read-only filesystem check failed to run."
            )
        elif online_iso is not None and severity == SEV_OK:
            try:
                last_run = datetime.fromisoformat(online_iso)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                age_days = (
                    datetime.now(timezone.utc) - last_run.astimezone(timezone.utc)
                ).days
                if age_days >= _WARN_DAYS_SINCE_ONLINE_CHECK:
                    severity = SEV_WARN
                    messages.append(
                        f"Read-only filesystem check hasn't run in {age_days} days."
                    )
            except ValueError:
                pass

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


def _local_iso(dt_naive: datetime) -> str:
    """Treat ``dt_naive`` as system local time and return an ISO-8601 string
    with the appropriate UTC offset (handles historical DST correctly via
    ``astimezone``).
    """
    # ``astimezone`` on a naive datetime assumes the system local TZ and
    # returns an aware datetime with the offset that was in effect at
    # that moment (DST-correct for the historical date).
    return dt_naive.astimezone().isoformat()


def _tune2fs_date_to_iso(value: str) -> str | None:
    """Parse e.g. ``Sun Apr 14 02:11:42 2024`` → ISO with local offset.

    tune2fs prints wall-clock time in the system's local timezone, so
    we re-attach the local offset to make the value unambiguous for
    the client.
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
        return _local_iso(datetime(year, month, day, hour, minute, second))
    except (TypeError, ValueError):
        return None


_SYSTEMCTL_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<weekday>[A-Za-z]{3})\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})"
)


def _systemctl_date_to_iso(value: str) -> str | None:
    """``Sun 2026-05-24 07:50:01 EDT`` → ISO with local offset.

    The printed TZ abbreviation is dropped (Python's stdlib can't parse
    e.g. ``EDT`` portably); the wall-clock value is in the system local
    TZ so we re-attach the offset here.
    """
    match = _SYSTEMCTL_DATE_RE.match((value or "").strip())
    if match is None:
        return None
    try:
        year, month, day = (int(x) for x in match.group("date").split("-"))
        hour, minute, second = (int(x) for x in match.group("time").split(":"))
        return _local_iso(datetime(year, month, day, hour, minute, second))
    except (TypeError, ValueError):
        return None


def make_storage_health_service(cfg: WebConfig) -> StorageHealthService:
    """Build the production-defaults service.

    Reuses ``cfg.samba.sudo_prefix`` so the operator only has to
    configure their privileged-command policy in one place
    (``[samba] sudo_prefix``); the storage probes need exactly the
    same NOPASSWD policy.

    On construction we opportunistically call
    :meth:`StorageHealthService.cleanup_after_fsck_boot` — if a prior
    process armed a one-shot root-fs fsck and we've since rebooted,
    this strips ``fsck.mode=force`` from the kernel cmdline so the
    *next* boot is fast again. Idempotent; safe to call every startup.
    """
    service = StorageHealthService(
        StorageHealthServiceConfig(sudo_prefix=cfg.samba.sudo_prefix)
    )
    try:
        service.cleanup_after_fsck_boot()
    except Exception:  # noqa: BLE001 — never block app startup
        logger.exception(
            "storage_health: cleanup_after_fsck_boot raised during startup"
        )
    return service


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
