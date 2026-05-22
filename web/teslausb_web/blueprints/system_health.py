"""System Health blueprint — single-poll snapshot for the Settings card.

Ports the v1 ``/api/system/health`` JSON contract so the existing
client-side card + nav-bar status dot work unchanged after the B-1
cutover. The subsystems probed here are the B-1 set:

* ``disk`` — free / total bytes on the data root that holds the
  TeslaCam + media trees. The root may be a btrfs subvolume parent
  (when the SD layout is btrfs) or a plain ext4 directory (when it
  isn't); ``os.statvfs`` answers either way.
* ``daemon`` — ``teslafat`` daemon state via the Unix-socket IPC client
  built in Phase 5.5 (replaces v1's fsck/IMG widget; when the data
  root is btrfs an additional scrub widget may be wired in later).
* ``samba`` — whether the Samba feature is enabled in config. Full
  service-up probe lands in Phase 5.17 alongside ``samba_service``.

Design rules (from v1):

* **Cheap.** No subprocesses on the hot path. Daemon probe is one
  Unix-socket round-trip (sub-millisecond locally).
* **Fault-tolerant.** Any subsystem that raises is reported as
  ``severity: "unknown"`` with a one-line error; the page always
  renders.
* **Stable shape.** Every subsystem block has ``severity`` and
  ``message``; the dot colours itself from ``severity`` alone.
* **No identifier disclosure.** ``message`` strings stay short and
  user-facing — no absolute paths, no secret leaks (charter
  Pillar 3, mirrored from Failed Jobs page).
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import time
import tomllib
from http import HTTPStatus
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Final

from flask import Blueprint, current_app, jsonify

from teslausb_web.services.gadget_state import gadget_mode_token
from teslausb_web.services.system_metrics import (
    collect_metrics,
    metrics_to_dict,
)
from teslausb_web.services.teslafat_client import (
    IpcDaemonError,
    IpcProtocolError,
    TeslaFatClient,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.typing import ResponseReturnValue

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

SEV_OK: Final[str] = "ok"
SEV_WARN: Final[str] = "warn"
SEV_ERROR: Final[str] = "error"
SEV_UNKNOWN: Final[str] = "unknown"

_SEV_RANK: Final[dict[str, int]] = {
    SEV_OK: 0,
    SEV_UNKNOWN: 1,
    SEV_WARN: 2,
    SEV_ERROR: 3,
}

# Free-space thresholds. ``CRITICAL`` < ``WARNING``; both in megabytes.
# Defaults match v1's ``cloud_archive.disk_space_*`` so the System
# Health card uses identical numbers regardless of underlying worker.
_DISK_WARNING_MB: Final[int] = 500
_DISK_CRITICAL_MB: Final[int] = 100
_BYTES_PER_MIB: Final[int] = 1024 * 1024

_MESSAGE_MAX_CHARS: Final[int] = 120

system_health_bp = Blueprint("system_health", __name__)


def _truncate(message: str) -> str:
    if len(message) <= _MESSAGE_MAX_CHARS:
        return message
    return message[: _MESSAGE_MAX_CHARS - 1] + "…"


def _disk_block(cfg: WebConfig) -> dict[str, object]:
    """Free-space probe on the data root (filesystem-agnostic)."""
    target = cfg.paths.backing_root
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        logger.warning("disk probe failed for %s: %s", target, exc)
        return {
            "severity": SEV_UNKNOWN,
            "message": "Disk usage probe failed",
        }

    free_mb = usage.free // _BYTES_PER_MIB
    used_pct = (usage.used / usage.total * 100) if usage.total else 0.0

    if free_mb < _DISK_CRITICAL_MB:
        severity = SEV_ERROR
        message = f"Critical: only {free_mb} MB free"
    elif free_mb < _DISK_WARNING_MB:
        severity = SEV_WARN
        message = f"Low: only {free_mb} MB free"
    else:
        severity = SEV_OK
        message = f"{free_mb:,} MB free ({used_pct:.0f}% used)"

    return {
        "severity": severity,
        "message": message,
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_pct": round(used_pct, 1),
    }


# Per-LUN TeslaFAT daemon configs live alongside the web app config.
# Each instance N has a ``teslafat-N.toml`` declaring its volume label
# and filesystem type (exfat for TeslaCam, fat32 for media). The matching
# systemd unit is ``teslafat@N.service``.
_TESLAFAT_CONFIG_DIR: Final[Path] = Path("/etc/teslausb")


def _read_teslafat_lun_config(lun_index: int) -> dict[str, str]:
    """Return ``{volume_label, fs_type}`` for LUN N, or empty dict on error."""
    path = _TESLAFAT_CONFIG_DIR / f"teslafat-{lun_index}.toml"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    out: dict[str, str] = {}
    label = data.get("volume_label")
    if isinstance(label, str):
        out["volume_label"] = label
    fs_type = data.get("fs_type")
    if isinstance(fs_type, str):
        out["fs_type"] = fs_type
    return out


def _teslafat_lun_block(lun_index: int) -> dict[str, object]:
    """Per-LUN TeslaFAT daemon probe — systemd unit + LUN config."""
    cfg_data = _read_teslafat_lun_config(lun_index)
    label = cfg_data.get("volume_label", f"LUN {lun_index}")
    fs_type = cfg_data.get("fs_type", "?")
    fs_display = {"exfat": "exFAT", "fat32": "FAT32"}.get(fs_type.lower(), fs_type)
    unit = f"teslafat@{lun_index}.service"
    state = _systemctl_is_active(unit)
    if state is None:
        return {
            "severity": SEV_UNKNOWN,
            "message": _truncate(f"Cannot probe {unit}"),
            "lun_id": lun_index,
            "volume_label": label,
            "fs_type": fs_type,
            "unit": unit,
        }
    if state == "active":
        severity = SEV_OK
        message = f"Active — {label} ({fs_display})"
    elif state in {"activating", "reloading"}:
        severity = SEV_WARN
        message = f"{state.title()} — {label} ({fs_display})"
    else:
        severity = SEV_ERROR
        message = _truncate(f"{state} — {label} ({fs_display})")
    return {
        "severity": severity,
        "message": message,
        "state": state,
        "lun_id": lun_index,
        "volume_label": label,
        "fs_type": fs_type,
        "unit": unit,
    }


def _teslafat_ipc_block(cfg: WebConfig) -> dict[str, object] | None:
    """Optional IPC-server probe — only used once ``ipc_daemon_enabled``.

    The phase-7 IPC server, when wired up, exposes richer state
    (SERVING/DRAINING, uptime). We surface that as a separate detail
    only when the feature flag is on; otherwise the per-LUN systemd
    probes above are the source of truth.
    """
    if not cfg.features.ipc_daemon_enabled:
        return None
    client = TeslaFatClient(cfg.paths.ipc_socket)
    try:
        body = client.status()
    except FileNotFoundError:
        return {"severity": SEV_ERROR, "message": "Daemon socket missing"}
    except (ConnectionError, TimeoutError, BlockingIOError) as exc:
        return {"severity": SEV_ERROR, "message": _truncate(f"Daemon unreachable: {exc}")}
    except IpcDaemonError as exc:
        return {"severity": SEV_WARN, "message": _truncate(f"Daemon error: {exc.body.code}")}
    except IpcProtocolError as exc:
        return {"severity": SEV_UNKNOWN, "message": _truncate(f"Protocol error: {exc}")}
    severity_for_state: dict[str, str] = {
        "SERVING": SEV_OK,
        "INITIALIZING": SEV_WARN,
        "DRAINING": SEV_WARN,
        "STOPPED": SEV_ERROR,
    }
    severity = severity_for_state.get(body.state, SEV_UNKNOWN)
    return {
        "severity": severity,
        "message": f"{body.state.title()} — LUN {body.lun_id} ({body.volume_label})",
        "state": body.state,
        "lun_id": body.lun_id,
        "volume_label": body.volume_label,
        "uptime_seconds": body.uptime_seconds,
    }


def _samba_block(cfg: WebConfig) -> dict[str, object]:
    """Samba feature flag — full service probe lands in Phase 5.17."""
    if cfg.features.samba_enabled:
        return {"severity": SEV_OK, "message": "Enabled"}
    return {"severity": SEV_OK, "message": "Disabled"}


# Cheap kernel/configfs/systemd probes that backstop the structured
# subsystem probes above. The point: *any* error anywhere in the
# B-1 stack must show up here, so the operator never has to SSH
# in to discover that the indexer DB was readonly for six hours
# (the bug that motivated this whole block — see LEARNINGS Phase 6).


def _gadget_block(cfg: WebConfig) -> dict[str, object]:
    """USB gadget presentation to the Tesla.

    "Present" means the UDC is bound AND both mass-storage LUNs have
    non-empty backing files. Anything less is reported as ERROR — the
    Tesla cannot see the drive in that state, which is the worst
    user-visible outcome we can have.
    """
    del cfg
    try:
        token = gadget_mode_token()
    except Exception as exc:  # noqa: BLE001 — must never raise
        return {
            "severity": SEV_UNKNOWN,
            "message": _truncate(f"Gadget probe failed: {exc}"),
        }
    if token == "present":
        return {
            "severity": SEV_OK,
            "message": "USB drives presented to Tesla",
            "token": token,
        }
    return {
        "severity": SEV_ERROR,
        "message": "Tesla cannot see USB drives (gadget unbound or LUN missing)",
        "token": token,
    }


def _indexer_block(cfg: WebConfig) -> dict[str, object]:
    """Worker SQLite index: writable + has recent rows.

    Opens the DB read-write and runs a no-op write inside a rolled-back
    transaction. Catches:

    * file ownership / permission drift (e.g. DB owned by ``pi`` when
      the worker runs as ``teslausb`` — the bug that triggered this
      probe);
    * read-only remount of /var/lib;
    * SQLite WAL lock-file ownership mismatch;
    * outright DB corruption (``PRAGMA quick_check``).

    Also reports last-indexed-clip timestamp + total clip count so the
    operator can spot a silent indexer stall ("DB is fine but nothing
    new has landed in 30 minutes").
    """
    db_path = cfg.paths.db_path
    if not db_path.exists():
        return {
            "severity": SEV_WARN,
            "message": "Index database not yet created",
        }
    try:
        # ``uri=true`` is intentional — keeps options explicit. ``mode=rw``
        # forces a writable open so a readonly mount surfaces immediately
        # rather than silently downgrading.
        conn = sqlite3.connect(
            f"file:{db_path}?mode=rw",
            uri=True,
            timeout=2.0,
        )
    except sqlite3.OperationalError as exc:
        return {
            "severity": SEV_ERROR,
            "message": _truncate(f"Index DB not writable: {exc}"),
        }
    try:
        with conn:
            check = conn.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                return {
                    "severity": SEV_ERROR,
                    "message": _truncate(f"Index DB corrupt: {check}"),
                }
            # Probe write capability without leaving a trace.
            conn.execute("CREATE TEMP TABLE _health_probe(x INTEGER)")
            conn.execute("INSERT INTO _health_probe VALUES (1)")
            conn.execute("DROP TABLE _health_probe")
            # Read indexer metrics — these tables exist after the first
            # migration so missing-table is itself a signal.
            try:
                count_row = conn.execute("SELECT COUNT(*) FROM clips").fetchone()
                clip_count = int(count_row[0]) if count_row else 0
            except sqlite3.OperationalError:
                clip_count = 0
            try:
                last_row = conn.execute(
                    "SELECT MAX(indexed_at_utc) FROM clips",
                ).fetchone()
                last_indexed = int(last_row[0]) if last_row and last_row[0] else 0
            except sqlite3.OperationalError:
                last_indexed = 0
    except sqlite3.DatabaseError as exc:
        return {
            "severity": SEV_ERROR,
            "message": _truncate(f"Index DB error: {exc}"),
        }
    finally:
        conn.close()

    now = int(time.time())
    age_s = (now - last_indexed) if last_indexed else None

    if clip_count == 0:
        return {
            "severity": SEV_WARN,
            "message": "Index is empty — no clips indexed yet",
            "clip_count": 0,
            "last_indexed_utc": 0,
        }
    if age_s is not None and age_s > 30 * 60:
        return {
            "severity": SEV_WARN,
            "message": f"{clip_count:,} clips indexed; newest is {age_s // 60} min old",
            "clip_count": clip_count,
            "last_indexed_utc": last_indexed,
        }
    return {
        "severity": SEV_OK,
        "message": f"{clip_count:,} clips indexed",
        "clip_count": clip_count,
        "last_indexed_utc": last_indexed,
    }


def _systemctl_is_active(unit: str) -> str | None:
    """Return ``systemctl is-active <unit>`` output or ``None`` on error.

    Used to probe worker / network / gadget units. We deliberately do
    not require sudo — ``is-active`` is unprivileged and runs in <50 ms
    on the Pi.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _worker_block(cfg: WebConfig) -> dict[str, object]:
    """``teslausb-worker`` systemd unit health."""
    del cfg
    state = _systemctl_is_active("teslausb-worker.service")
    if state is None:
        return {
            "severity": SEV_UNKNOWN,
            "message": "Cannot reach systemd to probe worker",
        }
    if state == "active":
        return {"severity": SEV_OK, "message": "Worker active", "state": state}
    if state == "activating":
        return {"severity": SEV_WARN, "message": "Worker starting", "state": state}
    return {
        "severity": SEV_ERROR,
        "message": f"Worker {state}",
        "state": state,
    }


def _network_block(cfg: WebConfig) -> dict[str, object]:
    """WiFi connectivity via NetworkManager.

    ``nmcli -t -f STATE general`` returns ``connected`` /
    ``connecting`` / ``disconnected``. We mirror that to severity.
    AP-mode (captive portal) reports as ``warn`` because the Tesla
    can't reach upstream cloud destinations in that mode.
    """
    del cfg
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "STATE", "general"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {
            "severity": SEV_UNKNOWN,
            "message": _truncate(f"Network probe failed: {exc}"),
        }
    state = result.stdout.strip()
    if state == "connected":
        return {"severity": SEV_OK, "message": "WiFi connected", "state": state}
    if state in {"connecting", "connecting (prepare)", "connecting (configuring)"}:
        return {"severity": SEV_WARN, "message": "WiFi connecting", "state": state}
    if state in {"connected (local only)", "connected (site only)"}:
        return {
            "severity": SEV_WARN,
            "message": f"WiFi {state} — captive portal or no upstream",
            "state": state,
        }
    return {
        "severity": SEV_ERROR,
        "message": f"WiFi {state or 'unreachable'}",
        "state": state,
    }


def _storage_writable_block(cfg: WebConfig) -> dict[str, object]:
    """Touch-probe every directory we depend on for writes.

    Even if `_disk_block` says there's free space and `_indexer_block`
    says the DB is fine, a stale ACL or RO-bind-mount on one of the
    other roots can silently break archiving or the cleanup pass.
    Catches that class of bug by writing (and immediately removing)
    a marker file in each one.
    """
    targets = (
        ("backing_root", cfg.paths.backing_root),
        ("state_dir", cfg.paths.state_dir),
    )
    bad: list[str] = []
    for name, target in targets:
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / f".health-probe.{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            bad.append(f"{name}: {exc.strerror or exc}")
    if not bad:
        return {"severity": SEV_OK, "message": "All write roots OK"}
    return {
        "severity": SEV_ERROR,
        "message": _truncate("; ".join(bad)),
    }


# Module-level cache for the journal probe. journalctl is the most
# expensive thing in this file (~200-400 ms cold). Health card polls
# every 30 s and the dot polls in parallel, so caching for 15 s keeps
# the endpoint well under its 100 ms budget without ever masking a
# fresh error for more than one poll cycle.
_JOURNAL_TTL_SECONDS: Final[float] = 15.0
_JOURNAL_LOOKBACK_SECONDS: Final[int] = 600  # 10 min
_JOURNAL_UNITS: Final[tuple[str, ...]] = (
    "teslafat@0.service",
    "teslafat@1.service",
    "nbd-attach@0.service",
    "nbd-attach@1.service",
    "usb-gadget.service",
    "teslausb-worker.service",
    "teslausb-web.service",
    "nginx.service",
)

_journal_cache: dict[str, object] = {"at": 0.0, "result": None}
_journal_lock = Lock()


def _scan_journal_uncached() -> dict[str, object]:
    """Tail journalctl for recent error-priority lines across our units."""
    args = [
        "journalctl",
        "-p",
        "err",
        "--no-pager",
        "--since",
        f"{_JOURNAL_LOOKBACK_SECONDS} seconds ago",
        "--output",
        "short-iso",
    ]
    for unit in _JOURNAL_UNITS:
        args.extend(["-u", unit])
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=8.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {
            "severity": SEV_UNKNOWN,
            "message": _truncate(f"Journal probe failed: {exc}"),
        }
    lines = [ln for ln in result.stdout.splitlines() if ln and "-- No entries --" not in ln]
    if not lines:
        return {
            "severity": SEV_OK,
            "message": f"No errors in last {_JOURNAL_LOOKBACK_SECONDS // 60} min",
            "count": 0,
        }
    last = lines[-1]
    return {
        "severity": SEV_WARN,
        "message": _truncate(f"{len(lines)} recent error(s); latest: {last}"),
        "count": len(lines),
        "latest": _truncate(last),
    }


def _journal_block(cfg: WebConfig) -> dict[str, object]:
    """Cached journal-error scan; see ``_JOURNAL_TTL_SECONDS``."""
    del cfg
    now = time.monotonic()
    with _journal_lock:
        cached_at = float(_journal_cache["at"])
        if now - cached_at < _JOURNAL_TTL_SECONDS and _journal_cache["result"] is not None:
            return dict(_journal_cache["result"])  # type: ignore[arg-type]
    result = _scan_journal_uncached()
    with _journal_lock:
        _journal_cache["at"] = now
        _journal_cache["result"] = result
    return dict(result)


def _build_health(cfg: WebConfig) -> dict[str, object]:
    """Compose the full payload, isolating per-subsystem crashes."""
    blocks: tuple[tuple[str, Callable[[WebConfig], dict[str, object]]], ...] = (
        ("disk", _disk_block),
        ("teslafat_0", lambda _c: _teslafat_lun_block(0)),
        ("teslafat_1", lambda _c: _teslafat_lun_block(1)),
        ("samba", _samba_block),
        ("gadget", _gadget_block),
        ("indexer", _indexer_block),
        ("worker", _worker_block),
        ("network", _network_block),
        ("storage_writable", _storage_writable_block),
        ("journal", _journal_block),
    )

    payload: dict[str, object] = {}
    worst_severity = SEV_OK
    worst_message = ""
    worst_subsystem: str | None = None

    for name, probe in blocks:
        try:
            block = probe(cfg)
        except Exception as exc:  # one bad block must not 500 the dashboard.
            logger.exception("system_health: %s block crashed", name)
            block = {
                "severity": SEV_UNKNOWN,
                "message": _truncate(f"Block error: {exc}"),
            }
        payload[name] = block

        block_severity = str(block.get("severity", SEV_UNKNOWN))
        if _SEV_RANK.get(block_severity, 0) > _SEV_RANK.get(worst_severity, 0):
            worst_severity = block_severity
            worst_message = str(block.get("message", ""))
            worst_subsystem = name

    try:
        ipc_block = _teslafat_ipc_block(cfg)
    except Exception as exc:  # noqa: BLE001 — must never raise
        logger.exception("system_health: teslafat_ipc block crashed")
        ipc_block = {"severity": SEV_UNKNOWN, "message": _truncate(f"Block error: {exc}")}
    if ipc_block is not None:
        payload["teslafat_ipc"] = ipc_block
        block_severity = str(ipc_block.get("severity", SEV_UNKNOWN))
        if _SEV_RANK.get(block_severity, 0) > _SEV_RANK.get(worst_severity, 0):
            worst_severity = block_severity
            worst_message = str(ipc_block.get("message", ""))
            worst_subsystem = "teslafat_ipc"

    payload["overall"] = {
        "severity": worst_severity,
        "message": (
            f"{worst_subsystem}: {worst_message}"
            if worst_severity != SEV_OK and worst_subsystem is not None
            else "All systems normal"
        ),
        "subsystem": worst_subsystem,
    }
    payload["generated_at"] = int(time.time())
    return payload


@system_health_bp.route("/api/system/health", methods=["GET"])
def api_system_health() -> ResponseReturnValue:
    """Return one JSON snapshot of every B-1 background subsystem.

    Used by the Settings system-health card and the nav-bar status
    dot. Both poll on a fixed interval, so this endpoint MUST stay
    sub-100 ms.
    """
    cfg: WebConfig = current_app.config["teslausb_config"]
    return jsonify(_build_health(cfg))


@system_health_bp.route("/api/system/metrics")
def api_system_metrics() -> ResponseReturnValue:
    """Live system metrics — backs the dashboard "Live Metrics" card.

    Cheap host counters (`psutil` + ``shutil.disk_usage`` only). The
    JS at ``templates/index.html`` polls this on a 5 s interval while
    the tab is visible. Per-subsystem fields the B-1 worker has not
    yet exposed (``task_coordinator`` / ``queues`` / ``peek_cache``)
    are omitted; the JS renders an em dash for those tiles. Wiring
    them up is tracked under Phase 6 (Rust daemon IPC).
    """
    cfg: WebConfig = current_app.config["teslausb_config"]
    metrics = collect_metrics(cfg.paths.backing_root)
    return jsonify(metrics_to_dict(metrics)), HTTPStatus.OK


@system_health_bp.route("/api/system/clear_lost_clips", methods=["POST"])
def api_clear_lost_clips() -> ResponseReturnValue:
    """Clear lost-clips counter. Stub — B-1 source_gone tracking TBD."""
    # TODO(#issue-needed): wire lost-clips counter to B-1 archive worker, Phase 5.x
    return jsonify({"success": True, "deleted": 0}), HTTPStatus.OK


__all__ = ("system_health_bp",)
