"""Live system metrics collector — Phase 5.29 close-out.

Backs the dashboard's "Live Metrics" card (`/api/system/metrics`). The
service reads cheap `/proc`-style counters via `psutil` plus
`shutil.disk_usage`; no subprocesses, no DB I/O, sub-millisecond on a
Pi Zero 2 W. Designed for a 5-second poll interval.

Charter notes:

* Pure-Python, no Flask imports — services layer (Layering Rule,
  `docs/03-CODE-QUALITY-CHARTER.md`).
* One bad probe MUST NOT poison the whole response: every metric is
  wrapped in a typed `OSError` / `psutil.Error` handler that records a
  `warning` and leaves the field as `None` (or a safe default).
* Frozen `SystemMetrics` dataclass is the in-process model; the
  blueprint calls `metrics_to_dict()` to reshape into the wire JSON
  the existing `index.html` JS already consumes.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import psutil

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_BYTES_PER_MIB: Final[int] = 1024 * 1024
_BYTES_PER_KIB: Final[int] = 1024
# Sensor labels the Pi exposes for the SoC; psutil reports the first
# one that matches in `sensors_temperatures()`.
_CPU_TEMP_KEYS: Final[tuple[str, ...]] = ("cpu_thermal", "soc_thermal", "coretemp", "cpu-thermal")


@dataclass(frozen=True, slots=True)
class IOSample:
    """Per-device read/write rates in kilobytes per second.

    `read_kbs` / `write_kbs` are *instantaneous* deltas against the
    previous `collect_metrics()` call. `avg60_read_kbs` /
    `avg60_write_kbs` are rolling averages over the last ~60 seconds
    of samples, which smooth over Tesla's bursty USB write pattern
    (each ~1-min dashcam segment is flushed in a single short burst,
    leaving the instantaneous rate at 0 between bursts). The averages
    are `None` until enough history accumulates (≥5 s).
    """

    read_kbs: float
    write_kbs: float
    avg60_read_kbs: float | None = None
    avg60_write_kbs: float | None = None


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    """One snapshot of host-level resource usage.

    All numeric counters are absolute; rates (`disk_io`) are deltas
    computed against the previous `collect_metrics()` call. The first
    call after process start will report `0.0` rates because there is
    no baseline yet — this is by design (the alternative is sleeping
    inside the request).
    """

    cpu_percent: float
    cpu_count: int
    memory_total_bytes: int
    memory_used_bytes: int
    memory_available_bytes: int
    memory_percent: float
    swap_total_bytes: int
    swap_used_bytes: int
    swap_percent: float
    disk_total_bytes: int
    disk_used_bytes: int
    disk_free_bytes: int
    disk_percent: float
    cpu_temp_celsius: float | None
    uptime_seconds: int
    load_average: tuple[float, float, float] | None
    disk_io: dict[str, IOSample] = field(default_factory=dict)
    timestamp: str = ""
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# IO rate sampling
# ---------------------------------------------------------------------------
# psutil exposes monotonically-increasing byte counters per disk. To
# turn them into a KB/s rate we cache the last sample and divide by
# the wall-clock delta. Guarded by a lock because Flask's dev server
# may serve concurrent requests.
#
# In addition to the single-poll instantaneous rate, we keep a short
# rolling window of recent samples per device so we can also report a
# 60-second average. Tesla writes to the USB LUNs in bursts (each
# dashcam segment is buffered in the camera SoC and flushed in a
# short burst every ~30-60 s), so the instantaneous rate is 0 most
# of the time even when the system is healthy.

_IO_WINDOW_SECONDS: Final[float] = 60.0
# Keep ~10 s of headroom so the 60-second window is always covered
# even with sparse polling. Anything older is pruned each call.
_IO_WINDOW_RETAIN_SECONDS: Final[float] = _IO_WINDOW_SECONDS + 10.0
# Need at least this much elapsed history before we report an
# avg60 — otherwise the average is just the instantaneous rate by
# another name and would be misleading on the dashboard.
_IO_AVG_MIN_WINDOW_SECONDS: Final[float] = 5.0

_io_lock: threading.Lock = threading.Lock()
_io_last_sample: dict[str, tuple[float, int, int]] = {}
_io_history: dict[str, list[tuple[float, int, int]]] = {}


def _sample_disk_io() -> tuple[dict[str, IOSample], list[str]]:
    """Compute per-device KB/s rates against the prior snapshot."""
    warnings_out: list[str] = []
    try:
        counters = psutil.disk_io_counters(perdisk=True)
    except (psutil.Error, OSError, RuntimeError) as exc:
        warnings_out.append(f"disk_io unavailable: {exc.__class__.__name__}")
        return {}, warnings_out

    now = time.monotonic()
    rates: dict[str, IOSample] = {}
    with _io_lock:
        for device, snap in counters.items():
            read_bytes = int(snap.read_bytes)
            write_bytes = int(snap.write_bytes)
            previous = _io_last_sample.get(device)
            if previous is None:
                inst_read = 0.0
                inst_write = 0.0
            else:
                last_ts, last_read, last_write = previous
                dt = max(now - last_ts, 1e-3)
                inst_read = max(read_bytes - last_read, 0) / dt / _BYTES_PER_KIB
                inst_write = max(write_bytes - last_write, 0) / dt / _BYTES_PER_KIB
            _io_last_sample[device] = (now, read_bytes, write_bytes)

            history = _io_history.setdefault(device, [])
            history.append((now, read_bytes, write_bytes))
            cutoff = now - _IO_WINDOW_RETAIN_SECONDS
            # Prune in-place; histories stay short (≤ ~30 entries at
            # a 5 s poll cadence) so an O(n) scan is fine.
            while history and history[0][0] < cutoff:
                history.pop(0)

            avg_read: float | None = None
            avg_write: float | None = None
            # Find the oldest sample inside the 60 s window; use it
            # as the baseline for the average so the window length
            # adapts to whatever history is actually available.
            window_start = now - _IO_WINDOW_SECONDS
            baseline: tuple[float, int, int] | None = None
            for entry in history:
                if entry[0] >= window_start:
                    baseline = entry
                    break
            if baseline is not None:
                base_ts, base_read, base_write = baseline
                window_dt = now - base_ts
                if window_dt >= _IO_AVG_MIN_WINDOW_SECONDS:
                    avg_read = (
                        max(read_bytes - base_read, 0) / window_dt / _BYTES_PER_KIB
                    )
                    avg_write = (
                        max(write_bytes - base_write, 0) / window_dt / _BYTES_PER_KIB
                    )

            rates[device] = IOSample(
                read_kbs=inst_read,
                write_kbs=inst_write,
                avg60_read_kbs=avg_read,
                avg60_write_kbs=avg_write,
            )
    return rates, warnings_out


# ---------------------------------------------------------------------------
# Individual metric probes
# ---------------------------------------------------------------------------


def _read_cpu(warnings_out: list[str]) -> tuple[float, int]:
    try:
        percent = float(psutil.cpu_percent(interval=None))
        count = int(psutil.cpu_count(logical=True) or 1)
    except (psutil.Error, OSError) as exc:
        warnings_out.append(f"cpu unavailable: {exc.__class__.__name__}")
        return 0.0, 1
    return percent, count


def _read_memory(warnings_out: list[str]) -> tuple[int, int, int, float]:
    try:
        mem = psutil.virtual_memory()
    except (psutil.Error, OSError) as exc:
        warnings_out.append(f"memory unavailable: {exc.__class__.__name__}")
        return 0, 0, 0, 0.0
    return int(mem.total), int(mem.used), int(mem.available), float(mem.percent)


def _read_swap(warnings_out: list[str]) -> tuple[int, int, float]:
    try:
        swap = psutil.swap_memory()
    except (psutil.Error, OSError) as exc:
        warnings_out.append(f"swap unavailable: {exc.__class__.__name__}")
        return 0, 0, 0.0
    return int(swap.total), int(swap.used), float(swap.percent)


def _read_disk(target: Path, warnings_out: list[str]) -> tuple[int, int, int, float]:
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        warnings_out.append(f"disk unavailable: {exc.__class__.__name__}")
        return 0, 0, 0, 0.0
    pct = (usage.used / usage.total * 100.0) if usage.total else 0.0
    return int(usage.total), int(usage.used), int(usage.free), round(pct, 1)


def _read_cpu_temp(warnings_out: list[str]) -> float | None:
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is None:
        return None
    try:
        readings = sensors()
    except (psutil.Error, OSError, AttributeError, NotImplementedError) as exc:
        warnings_out.append(f"cpu_temp unavailable: {exc.__class__.__name__}")
        return None
    for key in _CPU_TEMP_KEYS:
        entries = readings.get(key)
        if entries:
            return float(entries[0].current)
    # Fallback: take the first sensor of any name (some kernels label
    # the SoC sensor by its driver, e.g. `bcm2835_thermal`).
    for entries in readings.values():
        if entries:
            return float(entries[0].current)
    return None


def _read_load(warnings_out: list[str]) -> tuple[float, float, float] | None:
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        return None
    try:
        one, five, fifteen = getloadavg()
    except OSError as exc:
        warnings_out.append(f"loadavg unavailable: {exc.__class__.__name__}")
        return None
    return float(one), float(five), float(fifteen)


def _read_uptime(warnings_out: list[str]) -> int:
    try:
        boot = float(psutil.boot_time())
    except (psutil.Error, OSError) as exc:
        warnings_out.append(f"uptime unavailable: {exc.__class__.__name__}")
        return 0
    return max(int(time.time() - boot), 0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_metrics(backing_root: Path) -> SystemMetrics:
    """Read every cheap host counter and assemble a `SystemMetrics`.

    `backing_root` is the directory whose mount point should be
    sampled for disk usage — same value `system_health` uses. Caller
    must not hold any lock; the function is internally serialised on
    the IO-sample cache only.
    """
    warnings_out: list[str] = []
    cpu_pct, cpu_count = _read_cpu(warnings_out)
    mem_total, mem_used, mem_avail, mem_pct = _read_memory(warnings_out)
    swap_total, swap_used, swap_pct = _read_swap(warnings_out)
    disk_total, disk_used, disk_free, disk_pct = _read_disk(backing_root, warnings_out)
    cpu_temp = _read_cpu_temp(warnings_out)
    load_avg = _read_load(warnings_out)
    uptime = _read_uptime(warnings_out)
    io_rates, io_warnings = _sample_disk_io()
    warnings_out.extend(io_warnings)

    return SystemMetrics(
        cpu_percent=round(cpu_pct, 1),
        cpu_count=cpu_count,
        memory_total_bytes=mem_total,
        memory_used_bytes=mem_used,
        memory_available_bytes=mem_avail,
        memory_percent=round(mem_pct, 1),
        swap_total_bytes=swap_total,
        swap_used_bytes=swap_used,
        swap_percent=round(swap_pct, 1),
        disk_total_bytes=disk_total,
        disk_used_bytes=disk_used,
        disk_free_bytes=disk_free,
        disk_percent=disk_pct,
        cpu_temp_celsius=cpu_temp,
        uptime_seconds=uptime,
        load_average=load_avg,
        disk_io=io_rates,
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        warnings=tuple(warnings_out),
    )


def metrics_to_dict(m: SystemMetrics) -> dict[str, object]:
    """Reshape `SystemMetrics` into the wire JSON `index.html` consumes.

    The v1 `index.html` already reads:
      `loadavg.{one,five,fifteen}`, `cpu_count`, `cpu_pct`,
      `memory.{mem_total_mb,mem_available_mb,mem_used_pct,
       swap_total_mb,swap_used_mb,swap_used_pct}`,
      `io.<device>.{read_kbs,write_kbs}`,
      `generated_at` (unix seconds), `uptime_seconds`.
    Additional B-1-only fields (`cpu_temp_celsius`, `disk`,
    `timestamp`, `warnings`) ride alongside and are ignored by the JS
    if not consumed.
    """
    load = m.load_average
    return {
        "loadavg": (
            {"one": load[0], "five": load[1], "fifteen": load[2]} if load is not None else None
        ),
        "cpu_count": m.cpu_count,
        "cpu_pct": m.cpu_percent,
        "memory": {
            "mem_total_mb": m.memory_total_bytes // _BYTES_PER_MIB,
            "mem_available_mb": m.memory_available_bytes // _BYTES_PER_MIB,
            "mem_used_pct": m.memory_percent,
            "swap_total_mb": m.swap_total_bytes // _BYTES_PER_MIB,
            "swap_used_mb": m.swap_used_bytes // _BYTES_PER_MIB,
            "swap_used_pct": m.swap_percent,
        },
        "disk": {
            "total_bytes": m.disk_total_bytes,
            "used_bytes": m.disk_used_bytes,
            "free_bytes": m.disk_free_bytes,
            "used_pct": m.disk_percent,
        },
        "io": {
            device: {
                "read_kbs": round(sample.read_kbs, 2),
                "write_kbs": round(sample.write_kbs, 2),
                "avg60_read_kbs": (
                    round(sample.avg60_read_kbs, 2)
                    if sample.avg60_read_kbs is not None
                    else None
                ),
                "avg60_write_kbs": (
                    round(sample.avg60_write_kbs, 2)
                    if sample.avg60_write_kbs is not None
                    else None
                ),
            }
            for device, sample in m.disk_io.items()
        },
        "cpu_temp_celsius": m.cpu_temp_celsius,
        "uptime_seconds": m.uptime_seconds,
        "generated_at": int(time.time()),
        "timestamp": m.timestamp,
        "platform": sys.platform,
        "warnings": list(m.warnings),
    }


__all__ = ("IOSample", "SystemMetrics", "collect_metrics", "metrics_to_dict")
