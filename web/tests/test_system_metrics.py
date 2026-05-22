"""Tests for `services/system_metrics` + the `/api/system/metrics` route.

Strategy: monkeypatch every `psutil` / `shutil` / `os` call so the tests
are deterministic and don't depend on the host. One integration test
hits the live Flask route through the test client to verify the wire
shape the JS at `templates/index.html` consumes.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import psutil
import pytest
from teslausb_web.app import create_app
from teslausb_web.config import (
    FeaturesSection,
    PathsSection,
    WebConfig,
    WebSection,
)
from teslausb_web.services import system_metrics
from teslausb_web.services.system_metrics import (
    IOSample,
    SystemMetrics,
    collect_metrics,
    metrics_to_dict,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask.testing import FlaskClient


class _FakeSwap:
    total = 1024 * 1024 * 1024
    used = 64 * 1024 * 1024
    percent = 6.25


class _FakeVMem:
    total = 4 * 1024 * 1024 * 1024
    used = 2 * 1024 * 1024 * 1024
    available = 2 * 1024 * 1024 * 1024
    percent = 50.0


class _FakeUsage:
    def __init__(self, total: int, used: int, free: int) -> None:
        self.total = total
        self.used = used
        self.free = free


class _FakeIO:
    def __init__(self, read_bytes: int, write_bytes: int) -> None:
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes


class _FakeTemp:
    def __init__(self, current: float) -> None:
        self.current = current


@pytest.fixture(autouse=True)
def _reset_io_cache() -> Iterator[None]:
    # Each test must start with an empty IO baseline.
    system_metrics._io_last_sample.clear()
    yield
    system_metrics._io_last_sample.clear()


@pytest.fixture
def _patched_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 25.0)  # noqa: ARG005
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)  # noqa: ARG005
    monkeypatch.setattr(psutil, "virtual_memory", _FakeVMem)
    monkeypatch.setattr(psutil, "swap_memory", _FakeSwap)
    monkeypatch.setattr(
        psutil,
        "disk_io_counters",
        lambda perdisk=False: (  # noqa: ARG005
            {
                "mmcblk0": _FakeIO(1_000_000, 500_000),
                "nbd0": _FakeIO(2_000_000, 1_000_000),
                "nbd1": _FakeIO(500_000, 0),
            }
        ),
    )
    monkeypatch.setattr(psutil, "boot_time", lambda: time.time() - 3600.0)
    monkeypatch.setattr(
        psutil,
        "sensors_temperatures",
        lambda: {"cpu_thermal": [_FakeTemp(48.5)]},
        raising=False,
    )
    monkeypatch.setattr(
        "teslausb_web.services.system_metrics.shutil.disk_usage",
        lambda _p: _FakeUsage(64_000_000_000, 16_000_000_000, 48_000_000_000),
    )


@pytest.mark.usefixtures("_patched_psutil")
def test_collect_metrics_populates_all_fields(tmp_path: Path) -> None:
    metrics = collect_metrics(tmp_path)
    assert isinstance(metrics, SystemMetrics)
    assert metrics.cpu_percent == 25.0
    assert metrics.cpu_count == 4
    assert metrics.memory_total_bytes == 4 * 1024 * 1024 * 1024
    assert metrics.memory_percent == 50.0
    assert metrics.swap_total_bytes == 1024 * 1024 * 1024
    assert metrics.swap_used_bytes == 64 * 1024 * 1024
    assert metrics.disk_total_bytes == 64_000_000_000
    assert metrics.disk_free_bytes == 48_000_000_000
    assert metrics.disk_percent == 25.0
    assert metrics.cpu_temp_celsius == 48.5
    assert metrics.uptime_seconds >= 3599
    assert metrics.timestamp.endswith("+00:00")
    assert metrics.warnings == ()


@pytest.mark.usefixtures("_patched_psutil")
def test_collect_metrics_handles_missing_temp_sensor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delattr(psutil, "sensors_temperatures", raising=False)
    metrics = collect_metrics(tmp_path)
    assert metrics.cpu_temp_celsius is None


@pytest.mark.usefixtures("_patched_psutil")
def test_collect_metrics_falls_back_when_temp_sensor_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _raise() -> dict[str, list[_FakeTemp]]:
        raise OSError("no sensors")

    monkeypatch.setattr(psutil, "sensors_temperatures", _raise, raising=False)
    metrics = collect_metrics(tmp_path)
    assert metrics.cpu_temp_celsius is None
    assert any("cpu_temp" in w for w in metrics.warnings)


@pytest.mark.usefixtures("_patched_psutil")
def test_collect_metrics_disk_error_records_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _raise(_p: Path) -> _FakeUsage:
        raise OSError("disk missing")

    monkeypatch.setattr("teslausb_web.services.system_metrics.shutil.disk_usage", _raise)
    metrics = collect_metrics(tmp_path)
    assert metrics.disk_total_bytes == 0
    assert any("disk" in w for w in metrics.warnings)


@pytest.mark.usefixtures("_patched_psutil")
def test_io_rates_zero_on_first_call_then_populate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_clock = iter([100.0, 105.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(fake_clock))

    first = collect_metrics(tmp_path)
    assert first.disk_io["mmcblk0"] == IOSample(read_kbs=0.0, write_kbs=0.0)

    # Second sample, 5 s later, counters doubled.
    monkeypatch.setattr(
        psutil,
        "disk_io_counters",
        lambda perdisk=False: (  # noqa: ARG005
            {
                "mmcblk0": _FakeIO(2_000_000, 1_000_000),
                "nbd0": _FakeIO(4_000_000, 2_000_000),
                "nbd1": _FakeIO(1_000_000, 0),
            }
        ),
    )
    second = collect_metrics(tmp_path)
    # 1 MB delta over 5 s ≈ 195 KiB/s
    assert second.disk_io["mmcblk0"].read_kbs == pytest.approx(195.3, abs=1.0)
    assert second.disk_io["mmcblk0"].write_kbs == pytest.approx(97.7, abs=1.0)


@pytest.mark.usefixtures("_patched_psutil")
def test_disk_io_counter_failure_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _raise(perdisk: bool = False) -> dict[str, _FakeIO]:  # noqa: FBT001, FBT002
        del perdisk
        raise psutil.Error("no counters")

    monkeypatch.setattr(psutil, "disk_io_counters", _raise)
    metrics = collect_metrics(tmp_path)
    assert metrics.disk_io == {}
    assert any("disk_io" in w for w in metrics.warnings)


@pytest.mark.usefixtures("_patched_psutil")
def test_collect_metrics_isolates_each_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One bad probe MUST NOT poison the response — every metric is isolated."""

    def _raise_psutil(*_a: object, **_k: object) -> None:
        raise psutil.Error("boom")

    monkeypatch.setattr(psutil, "cpu_percent", _raise_psutil)
    monkeypatch.setattr(psutil, "virtual_memory", _raise_psutil)
    monkeypatch.setattr(psutil, "swap_memory", _raise_psutil)
    monkeypatch.setattr(psutil, "boot_time", _raise_psutil)

    metrics = collect_metrics(tmp_path)

    assert metrics.cpu_percent == 0.0
    assert metrics.memory_total_bytes == 0
    assert metrics.swap_total_bytes == 0
    assert metrics.uptime_seconds == 0
    # Four separate probes failed → four separate warnings recorded.
    joined = " ".join(metrics.warnings)
    for token in ("cpu", "memory", "swap", "uptime"):
        assert token in joined


@pytest.mark.usefixtures("_patched_psutil")
def test_cpu_temp_uses_first_sensor_when_known_keys_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        psutil,
        "sensors_temperatures",
        lambda: {"bcm2835_thermal": [_FakeTemp(55.5)]},
        raising=False,
    )
    metrics = collect_metrics(tmp_path)
    assert metrics.cpu_temp_celsius == 55.5


@pytest.mark.usefixtures("_patched_psutil")
def test_cpu_temp_returns_none_when_no_sensors_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(psutil, "sensors_temperatures", dict, raising=False)
    metrics = collect_metrics(tmp_path)
    assert metrics.cpu_temp_celsius is None


@pytest.mark.usefixtures("_patched_psutil")
def test_load_average_oserror_records_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _raise() -> tuple[float, float, float]:
        raise OSError("loadavg missing")

    monkeypatch.setattr(system_metrics.os, "getloadavg", _raise, raising=False)
    metrics = collect_metrics(tmp_path)
    assert metrics.load_average is None
    assert any("loadavg" in w for w in metrics.warnings)


@pytest.mark.usefixtures("_patched_psutil")
def test_metrics_to_dict_matches_js_wire_shape(tmp_path: Path) -> None:
    metrics = collect_metrics(tmp_path)
    payload = metrics_to_dict(metrics)

    # Every key the index.html JS reads.
    assert payload["cpu_count"] == 4
    assert payload["cpu_pct"] == 25.0
    assert isinstance(payload["loadavg"], dict) or payload["loadavg"] is None

    memory = payload["memory"]
    assert isinstance(memory, dict)
    for key in (
        "mem_total_mb",
        "mem_available_mb",
        "mem_used_pct",
        "swap_total_mb",
        "swap_used_mb",
        "swap_used_pct",
    ):
        assert key in memory

    disk = payload["disk"]
    assert isinstance(disk, dict)
    assert disk["used_pct"] == 25.0

    io = payload["io"]
    assert isinstance(io, dict)
    assert "mmcblk0" in io
    assert "nbd0" in io
    assert "nbd1" in io
    assert set(io["mmcblk0"].keys()) == {"read_kbs", "write_kbs"}

    assert isinstance(payload["generated_at"], int)
    assert isinstance(payload["uptime_seconds"], int)
    assert isinstance(payload["warnings"], list)


def test_metrics_to_dict_serializes_none_load_average() -> None:
    metrics = SystemMetrics(
        cpu_percent=10.0,
        cpu_count=2,
        memory_total_bytes=1024 * 1024 * 1024,
        memory_used_bytes=512 * 1024 * 1024,
        memory_available_bytes=512 * 1024 * 1024,
        memory_percent=50.0,
        swap_total_bytes=0,
        swap_used_bytes=0,
        swap_percent=0.0,
        disk_total_bytes=1_000_000,
        disk_used_bytes=500_000,
        disk_free_bytes=500_000,
        disk_percent=50.0,
        cpu_temp_celsius=None,
        uptime_seconds=10,
        load_average=None,
    )
    payload = metrics_to_dict(metrics)
    assert payload["loadavg"] is None
    assert payload["cpu_temp_celsius"] is None


# ---------------------------------------------------------------------------
# Flask route integration
# ---------------------------------------------------------------------------


def _make_config(backing_root: Path) -> WebConfig:
    return WebConfig(
        web=WebSection(secret_key="x" * 32, max_upload_mb=8, max_chunk_mb=1),
        paths=PathsSection(backing_root=backing_root),
        features=FeaturesSection(),
        source_path=None,
    )


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    return create_app(_make_config(tmp_path)).test_client()


@pytest.mark.usefixtures("_patched_psutil")
def test_api_system_metrics_route_returns_populated_json(client: FlaskClient) -> None:
    response = client.get("/api/system/metrics")
    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)

    for key in (
        "cpu_count",
        "cpu_pct",
        "memory",
        "io",
        "disk",
        "uptime_seconds",
        "generated_at",
        "timestamp",
        "warnings",
    ):
        assert key in body, f"missing JS-required key: {key}"
    assert body["cpu_count"] == 4
    assert body["cpu_pct"] == 25.0
