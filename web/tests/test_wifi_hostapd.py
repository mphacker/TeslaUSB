"""Unit tests for the hostapd-based on-board AP helper."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from teslausb_web.services import wifi_hostapd
from teslausb_web.services.wifi_support import WifiCommandError, WifiConfigError

if TYPE_CHECKING:
    from pathlib import Path


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ----------------------- conf rendering -----------------------


def test_render_hostapd_conf_includes_required_fields() -> None:
    conf = wifi_hostapd.render_hostapd_conf(ssid="TeslaUSB", passphrase="hunter22", channel=6)
    assert "interface=wlan0_ap" in conf
    assert "ssid=TeslaUSB" in conf
    assert "channel=6" in conf
    assert "wpa_passphrase=hunter22" in conf
    assert "wpa_key_mgmt=WPA-PSK" in conf


def test_render_hostapd_conf_rejects_invalid_channel() -> None:
    with pytest.raises(WifiConfigError, match="channel"):
        wifi_hostapd.render_hostapd_conf(ssid="ok", passphrase="hunter22", channel=99)


def test_render_hostapd_conf_rejects_short_passphrase() -> None:
    with pytest.raises(WifiConfigError, match="passphrase"):
        wifi_hostapd.render_hostapd_conf(ssid="ok", passphrase="short", channel=6)


def test_render_hostapd_conf_rejects_empty_ssid() -> None:
    with pytest.raises(WifiConfigError, match="ssid"):
        wifi_hostapd.render_hostapd_conf(ssid="   ", passphrase="hunter22", channel=6)


def test_render_hostapd_conf_rejects_newlines() -> None:
    with pytest.raises(WifiConfigError, match="newline"):
        wifi_hostapd.render_hostapd_conf(ssid="ok\ninject", passphrase="hunter22", channel=6)


def test_render_dnsmasq_conf_has_captive_portal_redirect() -> None:
    conf = wifi_hostapd.render_dnsmasq_conf()
    assert "interface=wlan0_ap" in conf
    assert "dhcp-range=192.168.4.10,192.168.4.50,12h" in conf
    assert "address=/#/192.168.4.1" in conf


# ----------------------- PID-file probing -----------------------


def test_is_ap_running_false_when_pid_files_missing(tmp_path: Path) -> None:
    with (
        patch.object(wifi_hostapd, "HOSTAPD_PID", tmp_path / "missing-hostapd.pid"),
        patch.object(wifi_hostapd, "DNSMASQ_PID", tmp_path / "missing-dnsmasq.pid"),
    ):
        assert wifi_hostapd.is_ap_running() is False


def test_is_ap_running_false_when_pid_dead(tmp_path: Path) -> None:
    hostapd_pid = tmp_path / "hostapd.pid"
    dnsmasq_pid = tmp_path / "dnsmasq.pid"
    hostapd_pid.write_text("999999")
    dnsmasq_pid.write_text("999998")
    with (
        patch.object(wifi_hostapd, "HOSTAPD_PID", hostapd_pid),
        patch.object(wifi_hostapd, "DNSMASQ_PID", dnsmasq_pid),
        patch.object(wifi_hostapd, "_pid_alive", return_value=False),
    ):
        assert wifi_hostapd.is_ap_running() is False


def test_is_ap_running_true_when_both_alive(tmp_path: Path) -> None:
    hostapd_pid = tmp_path / "hostapd.pid"
    dnsmasq_pid = tmp_path / "dnsmasq.pid"
    hostapd_pid.write_text("123")
    dnsmasq_pid.write_text("456")
    with (
        patch.object(wifi_hostapd, "HOSTAPD_PID", hostapd_pid),
        patch.object(wifi_hostapd, "DNSMASQ_PID", dnsmasq_pid),
        patch.object(wifi_hostapd, "_pid_alive", return_value=True),
    ):
        assert wifi_hostapd.is_ap_running() is True


# ----------------------- channel resolution -----------------------


def test_current_sta_channel_parses_frequency() -> None:
    output = "Connected to xx:xx:xx:xx:xx:xx (on wlan0)\n\tfreq: 2437\n\tsignal: -50 dBm\n"
    with patch.object(subprocess, "run", return_value=_completed(stdout=output)):
        assert wifi_hostapd.current_sta_channel() == 6


def test_current_sta_channel_none_when_not_associated() -> None:
    with patch.object(subprocess, "run", return_value=_completed(returncode=1)):
        assert wifi_hostapd.current_sta_channel() is None


def test_resolve_ap_channel_falls_back_to_default() -> None:
    with patch.object(wifi_hostapd, "current_sta_channel", return_value=None):
        assert wifi_hostapd.resolve_ap_channel() == wifi_hostapd.AP_DEFAULT_CHANNEL


def test_resolve_ap_channel_prefers_sta_channel() -> None:
    with patch.object(wifi_hostapd, "current_sta_channel", return_value=11):
        assert wifi_hostapd.resolve_ap_channel() == 11


# ----------------------- start/stop -----------------------


def test_start_ap_validates_credentials_before_touching_radio() -> None:
    with (
        patch.object(wifi_hostapd, "_run_sudo") as run_mock,
        patch.object(wifi_hostapd, "ensure_virtual_interface") as ensure_mock,
        pytest.raises(WifiConfigError),
    ):
        wifi_hostapd.start_ap(ssid="ok", passphrase="short")
    run_mock.assert_not_called()
    ensure_mock.assert_not_called()


def test_start_ap_writes_confs_and_starts_daemons(tmp_path: Path) -> None:
    hostapd_conf = tmp_path / "hostapd.conf"
    dnsmasq_conf = tmp_path / "dnsmasq.conf"
    hostapd_pid = tmp_path / "hostapd.pid"
    dnsmasq_pid = tmp_path / "dnsmasq.pid"
    with (
        patch.object(wifi_hostapd, "HOSTAPD_CONF", hostapd_conf),
        patch.object(wifi_hostapd, "DNSMASQ_CONF", dnsmasq_conf),
        patch.object(wifi_hostapd, "HOSTAPD_PID", hostapd_pid),
        patch.object(wifi_hostapd, "DNSMASQ_PID", dnsmasq_pid),
        patch.object(wifi_hostapd, "is_ap_running", return_value=False),
        patch.object(wifi_hostapd, "ensure_virtual_interface") as ensure_mock,
        patch.object(wifi_hostapd, "resolve_ap_channel", return_value=6),
        patch.object(wifi_hostapd, "_run_sudo", return_value=_completed()) as sudo_mock,
    ):
        wifi_hostapd.start_ap(ssid="TeslaUSB", passphrase="hunter22")
    ensure_mock.assert_called_once()
    assert hostapd_conf.read_text().startswith("interface=wlan0_ap\n")
    assert "ssid=TeslaUSB" in hostapd_conf.read_text()
    assert "dhcp-range" in dnsmasq_conf.read_text()
    # dnsmasq launched before hostapd so DHCP is ready first
    invocations = [call.args[0][0] for call in sudo_mock.call_args_list]
    assert invocations.index("dnsmasq") < invocations.index("hostapd")


def test_start_ap_cleans_up_dnsmasq_when_hostapd_fails(tmp_path: Path) -> None:
    def fake_sudo(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "hostapd":
            raise WifiCommandError("hostapd crashed")
        return _completed()

    hostapd_conf = tmp_path / "hostapd.conf"
    dnsmasq_conf = tmp_path / "dnsmasq.conf"
    hostapd_pid = tmp_path / "hostapd.pid"
    dnsmasq_pid = tmp_path / "dnsmasq.pid"
    dnsmasq_pid.write_text("4242")
    stop_pid_calls: list[Path] = []

    def fake_stop_pid(path: Path) -> None:
        stop_pid_calls.append(path)

    with (
        patch.object(wifi_hostapd, "HOSTAPD_CONF", hostapd_conf),
        patch.object(wifi_hostapd, "DNSMASQ_CONF", dnsmasq_conf),
        patch.object(wifi_hostapd, "HOSTAPD_PID", hostapd_pid),
        patch.object(wifi_hostapd, "DNSMASQ_PID", dnsmasq_pid),
        patch.object(wifi_hostapd, "is_ap_running", return_value=False),
        patch.object(wifi_hostapd, "ensure_virtual_interface"),
        patch.object(wifi_hostapd, "resolve_ap_channel", return_value=6),
        patch.object(wifi_hostapd, "_run_sudo", side_effect=fake_sudo),
        patch.object(wifi_hostapd, "_stop_pid", side_effect=fake_stop_pid),
        pytest.raises(WifiCommandError, match="hostapd"),
    ):
        wifi_hostapd.start_ap(ssid="TeslaUSB", passphrase="hunter22")
    assert dnsmasq_pid in stop_pid_calls


def test_stop_ap_kills_both_pids_and_removes_interface(tmp_path: Path) -> None:
    hostapd_pid = tmp_path / "hostapd.pid"
    dnsmasq_pid = tmp_path / "dnsmasq.pid"
    hostapd_pid.write_text("123")
    dnsmasq_pid.write_text("456")
    with (
        patch.object(wifi_hostapd, "HOSTAPD_PID", hostapd_pid),
        patch.object(wifi_hostapd, "DNSMASQ_PID", dnsmasq_pid),
        patch.object(wifi_hostapd, "_pid_alive", return_value=True),
        patch.object(wifi_hostapd, "remove_virtual_interface") as remove_mock,
        patch.object(wifi_hostapd, "_run_sudo", return_value=_completed()) as sudo_mock,
    ):
        wifi_hostapd.stop_ap()
    kill_invocations = [
        call.args[0] for call in sudo_mock.call_args_list if call.args[0][0] == "kill"
    ]
    assert ["kill", "123"] in kill_invocations
    assert ["kill", "456"] in kill_invocations
    remove_mock.assert_called_once()
    assert not hostapd_pid.exists()
    assert not dnsmasq_pid.exists()


def test_ensure_virtual_interface_creates_when_missing() -> None:
    interface_exists = MagicMock(
        side_effect=lambda name: name == wifi_hostapd.AP_PHYSICAL_INTERFACE
    )
    with (
        patch.object(wifi_hostapd, "_interface_exists", interface_exists),
        patch.object(wifi_hostapd, "_run_sudo", return_value=_completed()) as sudo_mock,
    ):
        wifi_hostapd.ensure_virtual_interface()
    invocations = [call.args[0] for call in sudo_mock.call_args_list]
    assert any(
        argv[:3] == ["iw", "dev", wifi_hostapd.AP_PHYSICAL_INTERFACE]
        and "interface" in argv
        and "add" in argv
        for argv in invocations
    )
    assert any(argv[:2] == ["nmcli", "device"] for argv in invocations)
    assert any(
        argv == ["ip", "link", "set", wifi_hostapd.AP_VIRTUAL_INTERFACE, "up"]
        for argv in invocations
    )


def test_ensure_virtual_interface_raises_when_physical_missing() -> None:
    with (
        patch.object(wifi_hostapd, "_interface_exists", return_value=False),
        pytest.raises(WifiCommandError, match="Physical interface"),
    ):
        wifi_hostapd.ensure_virtual_interface()


# ----------------------- _run_sudo error mapping -----------------------


def test_run_sudo_raises_on_failure_when_check_true() -> None:
    with (
        patch.object(
            subprocess,
            "run",
            return_value=_completed(returncode=1, stderr="nope"),
        ),
        pytest.raises(WifiCommandError, match="rc=1"),
    ):
        wifi_hostapd._run_sudo(["iw", "dev", "wlan0", "info"])


def test_run_sudo_timeout_raises() -> None:
    with (
        patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="iw", timeout=1.0),
        ),
        pytest.raises(WifiCommandError, match="timed out"),
    ):
        wifi_hostapd._run_sudo(["iw", "dev", "wlan0", "info"])
