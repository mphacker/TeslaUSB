# ruff: noqa: RUF043, SIM117
"""Tests for ``teslausb_web.services.wifi_service``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from teslausb_web.config import (
    PathsSection,
    WebConfig,
    WifiBinaryPaths,
    WifiSection,
)
from teslausb_web.services.wifi_service import (
    SavedWifiNetwork,
    WifiCommandError,
    WifiConfig,
    WifiConfigError,
    WifiConnectionRequest,
    WifiCredentials,
    WifiError,
    WifiNetwork,
    WifiService,
    make_wifi_service,
)
from teslausb_web.services.wifi_service import (
    WifiBinaryPaths as ServiceBinaryPaths,
)


@pytest.fixture
def config(tmp_path: Path) -> WifiConfig:
    return WifiConfig(
        credentials_path=tmp_path / "wifi_credentials.json",
        ap_ssid="TeslaUSB-Setup",
        ap_passphrase="",
        ap_idle_timeout_seconds=60,
        binary_paths=ServiceBinaryPaths(),
    )


@pytest.fixture
def service(config: WifiConfig) -> WifiService:
    with patch.object(
        WifiService,
        "_load_ap_state",
        return_value={"requested_enabled": False, "restore_deadline": None},
    ):
        return WifiService(config)


def _completed(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["cmd"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _which_map(tmp_path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name in ("nmcli", "iwlist", "iwconfig", "wpa_cli"):
        binary = tmp_path / name
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        mapping[name] = str(binary)
    return mapping


def test_binary_paths_validate_rejects_empty() -> None:
    with pytest.raises(WifiConfigError, match="binary_paths.nmcli"):
        ServiceBinaryPaths(nmcli="").validate()


def test_wifi_config_validate_rejects_relative_credentials_path() -> None:
    with pytest.raises(WifiConfigError, match="credentials_path"):
        WifiConfig(credentials_path=Path("relative.json")).validate()


def test_wifi_config_validate_rejects_bad_ap_passphrase(tmp_path: Path) -> None:
    with pytest.raises(WifiConfigError, match="ap_passphrase"):
        WifiConfig(credentials_path=tmp_path / "creds.json", ap_passphrase="short").validate()


@pytest.mark.parametrize(
    ("ssid", "passphrase", "message"),
    [("", "", "ssid"), ("x" * 33, "", "ssid"), ("okay", "short", "passphrase")],
)
def test_connection_request_validate_rejects_bad_values(
    ssid: str, passphrase: str, message: str
) -> None:
    with pytest.raises(WifiConfigError, match=message):
        WifiConnectionRequest(ssid=ssid, passphrase=passphrase).validate()


def test_split_fields_handles_escaped_colons(service: WifiService) -> None:
    assert service._saved_connection_names is not None
    from teslausb_web.services.wifi_support import _split_nmcli_fields

    assert _split_nmcli_fields(r"yes:My\:Wifi:67") == ["yes", "My:Wifi", "67"]


def test_resolve_binary_uses_which(service: WifiService, tmp_path: Path) -> None:
    paths = _which_map(tmp_path)
    with patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get):
        assert service._resolve_binary("nmcli") == Path(paths["nmcli"])


def test_resolve_binary_raises_when_missing(service: WifiService) -> None:
    with patch("teslausb_web.services.wifi_state.shutil.which", return_value=None):
        with pytest.raises(WifiError, match="wifi.binary_paths"):
            service._resolve_binary("nmcli")


def test_run_wraps_timeout(service: WifiService, tmp_path: Path) -> None:
    paths = _which_map(tmp_path)
    with (
        patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get),
        patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["nmcli"], timeout=1)),
        pytest.raises(WifiCommandError, match="timed out"),
    ):
        service._run("nmcli", ["device", "status"], timeout=1)


def test_load_credentials_returns_empty_when_store_missing(service: WifiService) -> None:
    assert service._load_credentials() == {}


def test_load_credentials_rejects_invalid_json(service: WifiService, config: WifiConfig) -> None:
    config.credentials_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(WifiConfigError, match="Invalid Wi-Fi credentials JSON"):
        service._load_credentials()


def test_store_credentials_writes_sorted_json(service: WifiService, config: WifiConfig) -> None:
    service._store_credentials(WifiCredentials(ssid="Bravo", passphrase="secret"))
    service._store_credentials(WifiCredentials(ssid="alpha", passphrase=""))
    payload = json.loads(config.credentials_path.read_text(encoding="utf-8"))
    assert [entry["ssid"] for entry in payload] == ["alpha", "Bravo"]


def test_delete_credentials_removes_entry(service: WifiService, config: WifiConfig) -> None:
    service._store_credentials(WifiCredentials(ssid="alpha", passphrase="secret"))
    service._delete_credentials("alpha")
    assert json.loads(config.credentials_path.read_text(encoding="utf-8")) == []


def test_write_json_file_applies_posix_permissions(service: WifiService, tmp_path: Path) -> None:
    target = tmp_path / "payload.json"
    with patch("pathlib.Path.chmod") as chmod_mock:
        service._write_json_file(target, {"ok": True})
    assert target.exists()
    chmod_mock.assert_not_called()


def test_write_json_file_raises_on_replace_failure(service: WifiService, tmp_path: Path) -> None:
    with patch("pathlib.Path.replace", side_effect=OSError("boom")):
        with pytest.raises(WifiError, match="Failed to write"):
            service._write_json_file(tmp_path / "payload.json", {"ok": True})


def test_saved_connection_names_filters_wireless_only(service: WifiService, tmp_path: Path) -> None:
    paths = _which_map(tmp_path)
    with (
        patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get),
        patch(
            "subprocess.run", return_value=_completed(stdout="Home:802-11-wireless\nlan:ethernet\n")
        ),
    ):
        assert service._saved_connection_names() == {"Home"}


def test_scan_networks_nmcli_deduplicates_and_sorts(service: WifiService, tmp_path: Path) -> None:
    paths = _which_map(tmp_path)
    outputs = [
        _completed(),
        _completed(stdout="no:Slow:20:WPA2\nyes:Fast:90:WPA3\nno:Fast:40:WPA2\nno::15:\n"),
    ]
    with (
        patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get),
        patch("subprocess.run", side_effect=outputs),
    ):
        rows = service._scan_networks_nmcli(rescan=True)
    assert [row["ssid"] for row in rows] == ["Fast", "Slow"]
    assert rows[0]["signal_strength"] == 90


def test_scan_networks_iwlist_parses_cells(service: WifiService, tmp_path: Path) -> None:
    paths = _which_map(tmp_path)
    stdout = """
              Cell 01 - Address: 00:00:00:00:00:00
                        ESSID:\"Alpha\"
                        Quality=35/70  Signal level=-75 dBm
                        Encryption key:on
              Cell 02 - Address: 00:00:00:00:00:01
                        ESSID:\"OpenWiFi\"
                        Quality=60/70  Signal level=-50 dBm
                        Encryption key:off
    """
    with (
        patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get),
        patch("subprocess.run", return_value=_completed(stdout=stdout)),
    ):
        rows = service._scan_networks_iwlist(rescan=False)
    assert rows[0]["ssid"] == "OpenWiFi"
    assert rows[0]["secured"] is False
    assert rows[1]["secured"] is True


def test_list_available_networks_falls_back_to_iwlist(service: WifiService) -> None:
    with (
        patch.object(service, "_saved_networks_snapshot", return_value=()),
        patch.object(
            service, "_current_connection_details", return_value=(None, None, None, None, False)
        ),
        patch.object(service, "_scan_networks_nmcli", side_effect=WifiError("nmcli unavailable")),
        patch.object(
            service,
            "_scan_networks_iwlist",
            return_value=[
                {
                    "ssid": "Fallback",
                    "signal_strength": 20,
                    "secured": False,
                    "security": "open",
                    "active": False,
                }
            ],
        ),
    ):
        networks = service.list_available_networks(rescan=False)
    assert networks == (
        WifiNetwork(
            ssid="Fallback",
            signal_strength=20,
            secured=False,
            security="open",
            active=False,
            saved=False,
        ),
    )


def test_list_available_networks_raises_when_both_scans_fail(service: WifiService) -> None:
    with (
        patch.object(service, "_saved_networks_snapshot", return_value=()),
        patch.object(
            service, "_current_connection_details", return_value=(None, None, None, None, False)
        ),
        patch.object(service, "_scan_networks_nmcli", side_effect=WifiError("nmcli unavailable")),
        patch.object(service, "_scan_networks_iwlist", side_effect=WifiError("iwlist unavailable")),
        pytest.raises(WifiError, match="Could not scan"),
    ):
        service.list_available_networks(rescan=False)


def test_current_connection_nmcli_parses_active_wifi(service: WifiService, tmp_path: Path) -> None:
    paths = _which_map(tmp_path)
    outputs = [
        _completed(stdout="wlan0:wifi:connected:Home\n"),
        _completed(stdout="GENERAL.CONNECTION:Home\nIP4.ADDRESS[1]:192.168.1.50/24\n"),
        _completed(stdout="yes:Home:77\n"),
    ]
    with (
        patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get),
        patch("subprocess.run", side_effect=outputs),
    ):
        details = service._current_connection_nmcli()
    assert details == ("Home", "Home", 77, "192.168.1.50", True)


def test_current_connection_nmcli_marks_ap_as_not_connected(
    service: WifiService, tmp_path: Path
) -> None:
    paths = _which_map(tmp_path)
    outputs = [
        _completed(stdout="wlan0:wifi:connected:TeslaUSB-Setup AP\n"),
        _completed(stdout="GENERAL.CONNECTION:TeslaUSB-Setup AP\n"),
        _completed(stdout=""),
    ]
    with (
        patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get),
        patch("subprocess.run", side_effect=outputs),
    ):
        details = service._current_connection_nmcli()
    assert details[-1] is False


def test_current_connection_iwconfig_parses_signal(service: WifiService, tmp_path: Path) -> None:
    paths = _which_map(tmp_path)
    stdout = 'wlan0 IEEE 802.11  ESSID:"Guest"\n          Signal level=-56 dBm\n'
    with (
        patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get),
        patch("subprocess.run", return_value=_completed(stdout=stdout)),
    ):
        details = service._current_connection_iwconfig()
    assert details == ("Guest", "Guest", -56, None, True)


def test_get_status_builds_saved_networks_snapshot(service: WifiService) -> None:
    with (
        patch.object(
            service,
            "_current_connection_details",
            return_value=("Home", "Home", 80, "192.168.1.5", True),
        ),
        patch.object(service, "_current_ap_mode", return_value=MagicMock()),
        patch.object(
            service,
            "_saved_networks_snapshot",
            return_value=(
                SavedWifiNetwork(ssid="Home", security="WPA2", has_passphrase=True, active=True),
            ),
        ),
    ):
        status = service.get_status()
    assert status.connected is True
    assert status.saved_networks[0].ssid == "Home"


def test_saved_networks_snapshot_uses_credentials_and_nmcli(service: WifiService) -> None:
    with (
        patch.object(
            service, "_saved_connection_names", return_value={"Home", "TeslaUSB-Setup AP"}
        ),
        patch.object(
            service,
            "_load_credentials",
            return_value={
                "Home": WifiCredentials(ssid="Home", passphrase="secret", security="WPA2")
            },
        ),
    ):
        snapshot = service._saved_networks_snapshot(active_ssid="Home")
    assert snapshot == (
        SavedWifiNetwork(ssid="Home", security="WPA2", has_passphrase=True, active=True),
    )


def test_connect_uses_saved_profile_without_password(
    service: WifiService, config: WifiConfig
) -> None:
    config.credentials_path.write_text(
        json.dumps([{"ssid": "Home", "passphrase": "", "security": "WPA2"}]), encoding="utf-8"
    )
    with (
        patch.object(service, "_current_ap_mode", return_value=MagicMock(active=False)),
        patch.object(service, "_saved_connection_names", return_value={"Home"}),
        patch.object(service, "_run", return_value=_completed()),
        patch.object(
            service, "get_status", return_value=MagicMock(connected=True, current_ssid="Home")
        ),
        patch.object(service, "_security_for_ssid", return_value="WPA2"),
        patch.object(service, "_store_credentials") as store_mock,
        patch.object(service, "_save_ap_state") as save_state_mock,
        patch.object(service, "_schedule_restore_timer") as schedule_mock,
    ):
        status = service.connect(WifiConnectionRequest(ssid="Home"))
    assert status.current_ssid == "Home"
    store_mock.assert_called_once_with(WifiCredentials(ssid="Home", passphrase="", security="WPA2"))
    save_state_mock.assert_called_once_with(requested_enabled=False, restore_deadline=None)
    schedule_mock.assert_called_once_with(None)


def test_connect_arms_ap_restore_when_ap_was_active(service: WifiService) -> None:
    ap_mode = MagicMock(active=True)
    with (
        patch.object(service, "_current_ap_mode", return_value=ap_mode),
        patch.object(service, "_saved_connection_names", return_value=set()),
        patch.object(service, "_run", return_value=_completed()),
        patch.object(service, "_bring_ap_down") as bring_down_mock,
        patch.object(
            service, "get_status", return_value=MagicMock(connected=True, current_ssid="Home")
        ),
        patch.object(service, "_security_for_ssid", return_value="WPA2"),
        patch.object(service, "_store_credentials"),
        patch.object(service, "_save_ap_state") as save_state_mock,
        patch.object(service, "_schedule_restore_timer") as schedule_mock,
    ):
        service.connect(WifiConnectionRequest(ssid="Home", passphrase="supersecret"))
    bring_down_mock.assert_called_once()
    assert save_state_mock.call_count == 2
    assert schedule_mock.call_count == 2


def test_connect_raises_when_status_does_not_match_target(service: WifiService) -> None:
    with (
        patch.object(service, "_current_ap_mode", return_value=MagicMock(active=False)),
        patch.object(
            service, "_current_connection_details", return_value=(None, None, None, None, False)
        ),
        patch.object(service, "_saved_connection_names", return_value=set()),
        patch.object(service, "_run", return_value=_completed(returncode=10, stderr="failed")),
        patch.object(
            service, "get_status", return_value=MagicMock(connected=False, current_ssid=None)
        ),
        pytest.raises(WifiCommandError, match="Wi-Fi connect failed"),
    ):
        service.connect(WifiConnectionRequest(ssid="Home", passphrase="supersecret"))


def test_disconnect_uses_wpa_cli_fallback(service: WifiService) -> None:
    with (
        patch.object(
            service, "_run", side_effect=[WifiError("nmcli bad"), _completed(), _completed()]
        ),
        patch.object(service, "set_ap_mode") as set_ap_mode_mock,
        patch.object(service, "get_status", return_value=MagicMock()),
    ):
        service.disconnect()
    set_ap_mode_mock.assert_called_once_with(enabled=True)


def test_forget_network_enables_ap_when_offline(service: WifiService) -> None:
    with (
        patch.object(service, "_run", return_value=_completed()),
        patch.object(service, "_delete_credentials") as delete_mock,
        patch.object(
            service,
            "get_status",
            side_effect=[MagicMock(connected=False), MagicMock(connected=False)],
        ),
        patch.object(service, "set_ap_mode") as set_ap_mode_mock,
    ):
        service.forget_network("Home")
    delete_mock.assert_called_once_with("Home")
    set_ap_mode_mock.assert_called_once_with(enabled=True)


def test_forget_network_raises_on_nmcli_failure(service: WifiService) -> None:
    with patch.object(service, "_run", return_value=_completed(returncode=4, stderr="nope")):
        with pytest.raises(WifiCommandError, match="forget"):
            service.forget_network("Home")


def test_set_ap_mode_enables_and_clears_timer(service: WifiService) -> None:
    with (
        patch.object(service, "_bring_ap_up") as bring_up_mock,
        patch.object(service, "_save_ap_state") as save_state_mock,
        patch.object(service, "_schedule_restore_timer") as schedule_mock,
        patch.object(
            service,
            "_current_connection_details",
            return_value=("TeslaUSB-Setup AP", None, None, None, False),
        ),
        patch.object(service, "_current_ap_mode", return_value=MagicMock()),
    ):
        service.set_ap_mode(enabled=True)
    bring_up_mock.assert_called_once()
    save_state_mock.assert_called_once_with(requested_enabled=True, restore_deadline=None)
    schedule_mock.assert_called_once_with(None)


def test_set_ap_mode_disables_and_clears_timer(service: WifiService) -> None:
    with (
        patch.object(service, "_bring_ap_down") as bring_down_mock,
        patch.object(service, "_save_ap_state") as save_state_mock,
        patch.object(service, "_schedule_restore_timer") as schedule_mock,
        patch.object(
            service, "_current_connection_details", return_value=(None, None, None, None, False)
        ),
        patch.object(service, "_current_ap_mode", return_value=MagicMock()),
    ):
        service.set_ap_mode(enabled=False)
    bring_down_mock.assert_called_once()
    save_state_mock.assert_called_once_with(requested_enabled=False, restore_deadline=None)
    schedule_mock.assert_called_once_with(None)


def test_bring_ap_up_creates_profile_when_missing(service: WifiService) -> None:
    with (
        patch.object(service, "_saved_connection_names", return_value=set()),
        patch.object(service, "_run", return_value=_completed()) as run_mock,
    ):
        service._bring_ap_up()
    assert run_mock.call_count == 3


def test_bring_ap_down_allows_inactive_code(service: WifiService) -> None:
    with patch.object(service, "_run", return_value=_completed(returncode=10)):
        service._bring_ap_down()


def test_restore_ap_if_needed_enables_ap_when_offline(service: WifiService) -> None:
    with (
        patch.object(
            service,
            "_load_ap_state",
            return_value={
                "requested_enabled": False,
                "restore_deadline": "2020-01-01T00:00:00+00:00",
            },
        ),
        patch.object(service, "get_status", return_value=MagicMock(connected=False)),
        patch.object(service, "_bring_ap_up") as bring_up_mock,
        patch.object(service, "_save_ap_state") as save_state_mock,
    ):
        service._restore_ap_if_needed()
    bring_up_mock.assert_called_once()
    save_state_mock.assert_called_once_with(requested_enabled=True, restore_deadline=None)


def test_restore_ap_if_needed_clears_deadline_when_connected(service: WifiService) -> None:
    with (
        patch.object(
            service,
            "_load_ap_state",
            return_value={
                "requested_enabled": False,
                "restore_deadline": "2020-01-01T00:00:00+00:00",
            },
        ),
        patch.object(service, "get_status", return_value=MagicMock(connected=True)),
        patch.object(service, "_save_ap_state") as save_state_mock,
    ):
        service._restore_ap_if_needed()
    save_state_mock.assert_called_once_with(requested_enabled=False, restore_deadline=None)


def test_restore_ap_if_needed_reschedules_future_deadline(service: WifiService) -> None:
    with (
        patch.object(
            service,
            "_load_ap_state",
            return_value={
                "requested_enabled": False,
                "restore_deadline": "2999-01-01T00:00:00+00:00",
            },
        ),
        patch.object(service, "_schedule_restore_timer") as schedule_mock,
    ):
        service._restore_ap_if_needed()
    schedule_mock.assert_called_once()


def test_schedule_restore_timer_cancels_existing(service: WifiService) -> None:
    existing = MagicMock()
    service._restore_timer = existing
    with patch("teslausb_web.services.wifi_service.threading.Timer") as timer_type:
        service._schedule_restore_timer(None)
    existing.cancel.assert_called_once()
    timer_type.assert_not_called()


def test_load_ap_state_rejects_non_object_json(service: WifiService, config: WifiConfig) -> None:
    state_path = config.credentials_path.with_name("wifi_credentials_ap_state.json")
    state_path.write_text("[]", encoding="utf-8")
    with pytest.raises(WifiConfigError, match="AP state file"):
        service._load_ap_state()


def test_make_wifi_service_uses_web_config(tmp_path: Path) -> None:
    cfg = WebConfig(
        paths=PathsSection(backing_root=tmp_path / "backing", state_dir=tmp_path / "state"),
        wifi=WifiSection(
            credentials_path=tmp_path / "state" / "wifi.json",
            ap_ssid="MyAP",
            ap_passphrase="supersecret",
            ap_idle_timeout_seconds=120,
            binary_paths=WifiBinaryPaths(
                nmcli="/usr/bin/nmcli",
                iwlist="/usr/sbin/iwlist",
                iwconfig="/usr/sbin/iwconfig",
                wpa_cli="/usr/sbin/wpa_cli",
            ),
        ),
    )
    service = make_wifi_service(cfg)
    assert service._config.credentials_path == tmp_path / "state" / "wifi.json"
    assert service._config.ap_ssid == "MyAP"
    assert service._config.ap_passphrase == "supersecret"
    assert service._config.ap_idle_timeout_seconds == 120


def test_make_wifi_service_constructor_rejects_invalid_config() -> None:
    with pytest.raises(WifiConfigError, match="credentials_path"):
        WifiService(WifiConfig(credentials_path=Path("relative.json")))


def test_wifi_config_validate_rejects_blank_ap_ssid_and_idle_timeout(tmp_path: Path) -> None:
    with pytest.raises(WifiConfigError, match="ap_ssid"):
        WifiConfig(credentials_path=tmp_path / "creds.json", ap_ssid=" ").validate()
    with pytest.raises(WifiConfigError, match="ap_idle_timeout_seconds"):
        WifiConfig(credentials_path=tmp_path / "creds.json", ap_idle_timeout_seconds=0).validate()


def test_list_saved_networks_uses_active_connection(service: WifiService) -> None:
    with (
        patch.object(
            service, "_current_connection_details", return_value=("Home", "Home", 80, None, True)
        ),
        patch.object(
            service,
            "_saved_networks_snapshot",
            return_value=(
                SavedWifiNetwork(ssid="Home", security="WPA2", has_passphrase=True, active=True),
            ),
        ),
    ):
        saved = service.list_saved_networks()
    assert saved[0].active is True


def test_connect_logs_warning_when_ap_restore_deadline_remains(service: WifiService) -> None:
    with (
        patch.object(
            service, "_current_connection_details", return_value=(None, None, None, None, False)
        ),
        patch.object(service, "_current_ap_mode", return_value=MagicMock(active=True)),
        patch.object(service, "_bring_ap_down"),
        patch.object(service, "_saved_connection_names", return_value=set()),
        patch.object(
            service, "_run", return_value=_completed(returncode=9, stderr="bad passphrase")
        ),
        patch.object(
            service, "get_status", return_value=MagicMock(connected=False, current_ssid=None)
        ),
        patch("teslausb_web.services.wifi_service.logger.warning") as warning_mock,
        pytest.raises(WifiCommandError, match="Wi-Fi connect failed"),
    ):
        service.connect(WifiConnectionRequest(ssid="Home", passphrase="supersecret"))
    warning_mock.assert_called_once()


def test_disconnect_raises_when_fallback_disconnect_fails(service: WifiService) -> None:
    with patch.object(
        service,
        "_run",
        side_effect=[WifiError("nmcli bad"), _completed(returncode=5, stderr="still connected")],
    ):
        with pytest.raises(WifiCommandError, match="disconnect"):
            service.disconnect()


def test_forget_network_rejects_blank_ssid(service: WifiService) -> None:
    with pytest.raises(WifiConfigError, match="ssid"):
        service.forget_network("   ")


def test_current_connection_nmcli_raises_when_device_show_fails(
    service: WifiService, tmp_path: Path
) -> None:
    paths = _which_map(tmp_path)
    outputs = [
        _completed(stdout="wlan0:wifi:connected:Home\n"),
        _completed(returncode=4, stderr="no ip"),
    ]
    with (
        patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get),
        patch("subprocess.run", side_effect=outputs),
        pytest.raises(WifiCommandError, match="status"),
    ):
        service._current_connection_nmcli()


def test_current_connection_iwconfig_marks_ap_profile_disconnected(
    service: WifiService, tmp_path: Path
) -> None:
    paths = _which_map(tmp_path)
    stdout = 'wlan0 IEEE 802.11  ESSID:"TeslaUSB-Setup AP"\n          Signal level=-56 dBm\n'
    with (
        patch("teslausb_web.services.wifi_state.shutil.which", side_effect=paths.get),
        patch("subprocess.run", return_value=_completed(stdout=stdout)),
    ):
        details = service._current_connection_iwconfig()
    assert details[-1] is False


def test_current_ap_mode_uses_state_file(service: WifiService, config: WifiConfig) -> None:
    state_path = config.credentials_path.with_name("wifi_credentials_ap_state.json")
    state_path.write_text(
        '{"requested_enabled": true, "restore_deadline": "2025-01-01T00:00:00+00:00"}',
        encoding="utf-8",
    )
    ap_mode = service._current_ap_mode(connection_name="TeslaUSB-Setup AP")
    assert ap_mode.requested_enabled is True
    assert ap_mode.active is True
    assert ap_mode.restore_deadline is not None


def test_security_for_ssid_prefers_scanned_network(service: WifiService) -> None:
    with (
        patch.object(
            service,
            "list_available_networks",
            return_value=(
                WifiNetwork(
                    ssid="Home",
                    signal_strength=70,
                    secured=True,
                    security="WPA3",
                    active=False,
                    saved=False,
                ),
            ),
        ),
        patch.object(
            service,
            "_load_credentials",
            return_value={
                "Home": WifiCredentials(ssid="Home", passphrase="secret", security="WPA2")
            },
        ),
    ):
        assert service._security_for_ssid("Home") == "WPA3"


def test_scan_networks_nmcli_raises_when_rescan_fails(service: WifiService) -> None:
    with patch.object(service, "_run", return_value=_completed(returncode=4, stderr="scan denied")):
        with pytest.raises(WifiCommandError, match="rescan"):
            service._scan_networks_nmcli(rescan=True)


def test_saved_connection_names_raises_on_nmcli_failure(service: WifiService) -> None:
    with patch.object(service, "_run", return_value=_completed(returncode=8, stderr="nm down")):
        with pytest.raises(WifiCommandError, match="list saved connections"):
            service._saved_connection_names()


def test_bring_ap_up_reuses_existing_profile_for_open_ap(service: WifiService) -> None:
    with (
        patch.object(service, "_saved_connection_names", return_value={"TeslaUSB-Setup AP"}),
        patch.object(service, "_run", return_value=_completed()) as run_mock,
    ):
        service._bring_ap_up()
    assert run_mock.call_count == 2


def test_bring_ap_down_raises_when_nmcli_fails(service: WifiService) -> None:
    with patch.object(service, "_run", return_value=_completed(returncode=4, stderr="busy")):
        with pytest.raises(WifiCommandError, match="disable AP mode"):
            service._bring_ap_down()


def test_load_credentials_raises_for_non_list_payload(
    service: WifiService, config: WifiConfig
) -> None:
    config.credentials_path.write_text('{"ssid":"Home"}', encoding="utf-8")
    with pytest.raises(WifiConfigError, match="JSON array"):
        service._load_credentials()


def test_load_ap_state_raises_on_read_failure(service: WifiService) -> None:
    with (
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "read_text", side_effect=OSError("boom")),
    ):
        with pytest.raises(WifiError, match="AP state file"):
            service._load_ap_state()


def test_save_ap_state_writes_iso_deadline(service: WifiService) -> None:
    with patch.object(service._state_store, "save_ap_state") as save_mock:
        service._save_ap_state(requested_enabled=True, restore_deadline=None)
    save_mock.assert_called_once()


def test_helper_parsers_and_command_message() -> None:
    from teslausb_web.services.wifi_support import (
        _command_message,
        _nmcli_value,
        _parse_datetime,
        _to_int,
    )

    assert _command_message("scan", _completed(returncode=4)) == "Wi-Fi scan failed: exit code 4"
    assert _nmcli_value(" -- ") is None
    assert _parse_datetime("2025-01-01T00:00:00") is not None
    assert _to_int("7") == 7
    assert _to_int("nope") is None
