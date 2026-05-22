"""Wi-Fi / captive-portal service for the B-1 web app."""

from __future__ import annotations

import dataclasses
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from teslausb_web.services.wifi_state import WifiCommandRunner, WifiStateStore
from teslausb_web.services.wifi_support import (
    _CONNECT_TIMEOUT_SECONDS,
    _DEFAULT_INTERFACE,
    _SHORT_TIMEOUT_SECONDS,
    ApMode,
    ConnectionDetails,
    NetworkRow,
    SavedWifiNetwork,
    WifiBinaryPaths,
    WifiCommandError,
    WifiConfig,
    WifiConfigError,
    WifiConnectionRequest,
    WifiCredentials,
    WifiError,
    WifiNetwork,
    WifiStatus,
    _command_message,
    _parse_datetime,
    bring_ap_down,
    bring_ap_up,
    current_connection_iwconfig,
    current_connection_nmcli,
    saved_connection_names,
    saved_wifi_profile_ssids,
    scan_networks_iwlist,
    scan_networks_nmcli,
)

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

__all__ = [
    "ApMode",
    "SavedWifiNetwork",
    "WifiBinaryPaths",
    "WifiCommandError",
    "WifiConfig",
    "WifiConfigError",
    "WifiConnectionRequest",
    "WifiCredentials",
    "WifiError",
    "WifiNetwork",
    "WifiService",
    "WifiStatus",
    "make_wifi_service",
]


class WifiService:
    """Manage Wi-Fi scanning, connection state, credentials, and AP mode."""

    def __init__(self, config: WifiConfig) -> None:
        config.validate()
        self._config = self._apply_persisted_ap_overrides(config)
        self._state_store = WifiStateStore(self._config)
        self._runner = WifiCommandRunner(self._config)
        self._lock = threading.RLock()
        self._restore_timer: threading.Timer | None = None
        restore_deadline = self._load_ap_state().get("restore_deadline")
        self._schedule_restore_timer(_parse_datetime(restore_deadline))

    @staticmethod
    def _apply_persisted_ap_overrides(config: WifiConfig) -> WifiConfig:
        """Overlay any operator-saved AP SSID/passphrase onto `config`.

        The captive-portal "Update AP Credentials" form writes its
        override to a JSON file alongside `credentials_path`. Loading
        it here means a service restart picks up the operator's last
        choice instead of reverting to the toml/dataclass defaults.
        """
        overlay_store = WifiStateStore(config)
        try:
            overrides = overlay_store.load_ap_config()
        except (WifiConfigError, WifiError) as exc:
            logger.warning("Ignoring corrupt AP config override: %s", exc)
            return config
        if not overrides:
            return config
        try:
            updated = dataclasses.replace(
                config,
                ap_ssid=overrides["ssid"],
                ap_passphrase=overrides["passphrase"],
            )
            updated.validate()
        except WifiConfigError as exc:
            logger.warning("Ignoring invalid AP config override: %s", exc)
            return config
        return updated

    def get_status(self) -> WifiStatus:
        with self._lock:
            connection = self._current_connection_details()
            ap_mode = self._current_ap_mode(connection_name=connection[0])
            saved = self._saved_networks_snapshot(
                active_ssid=connection[1] if connection[4] else None
            )
            return WifiStatus(
                connected=connection[4],
                current_ssid=connection[1] if connection[4] else None,
                signal_strength=connection[2] if connection[4] else None,
                ip_address=connection[3] if connection[4] else None,
                ap_mode=ap_mode,
                saved_networks=saved,
            )

    def list_saved_networks(self) -> tuple[SavedWifiNetwork, ...]:
        with self._lock:
            active_ssid = self._current_connection_details()[1]
            return self._saved_networks_snapshot(active_ssid=active_ssid)

    def list_available_networks(self, *, rescan: bool = True) -> tuple[WifiNetwork, ...]:
        with self._lock:
            stored_ssids = {
                network.ssid for network in self._saved_networks_snapshot(active_ssid=None)
            }
            current_ssid = self._current_connection_details()[1]
            for loader in (self._scan_networks_nmcli, self._scan_networks_iwlist):
                try:
                    rows = loader(rescan=rescan)
                    return tuple(
                        WifiNetwork(
                            ssid=row["ssid"],
                            signal_strength=row["signal_strength"],
                            secured=row["secured"],
                            security=row["security"],
                            active=row["ssid"] == current_ssid,
                            saved=row["ssid"] in stored_ssids,
                        )
                        for row in rows
                    )
                except WifiError:
                    continue
            raise WifiError("Could not scan Wi-Fi networks with nmcli or iwlist")

    def connect(self, request: WifiConnectionRequest) -> WifiStatus:
        request.validate()
        with self._lock:
            current_ap_mode = self._current_ap_mode(
                connection_name=self._current_connection_details()[0]
            )
            if current_ap_mode.active:
                self._bring_ap_down()
                deadline = datetime.now(tz=UTC) + timedelta(
                    seconds=self._config.ap_idle_timeout_seconds
                )
                self._save_ap_state(requested_enabled=False, restore_deadline=deadline)
                self._schedule_restore_timer(deadline)
            passphrase = self._effective_passphrase(request)
            saved_names = self._saved_connection_names()
            if request.ssid in saved_names and not passphrase:
                result = self._run(
                    "nmcli",
                    ["connection", "up", request.ssid, "ifname", _DEFAULT_INTERFACE],
                    timeout=_CONNECT_TIMEOUT_SECONDS,
                )
            else:
                command = [
                    "device",
                    "wifi",
                    "connect",
                    request.ssid,
                    "ifname",
                    _DEFAULT_INTERFACE,
                    "name",
                    request.ssid,
                ]
                if passphrase:
                    command.extend(["password", passphrase])
                result = self._run("nmcli", command, timeout=_CONNECT_TIMEOUT_SECONDS)
                if result.returncode != 0 and request.ssid in saved_names:
                    result = self._run(
                        "nmcli",
                        ["connection", "up", request.ssid, "ifname", _DEFAULT_INTERFACE],
                        timeout=_CONNECT_TIMEOUT_SECONDS,
                    )
            status = self.get_status()
            if status.connected and status.current_ssid == request.ssid:
                self._store_credentials(
                    WifiCredentials(
                        ssid=request.ssid,
                        passphrase=passphrase,
                        security=self._security_for_ssid(request.ssid),
                    )
                )
                self._save_ap_state(requested_enabled=False, restore_deadline=None)
                self._schedule_restore_timer(None)
                return status
            if current_ap_mode.active:
                logger.warning(
                    "Failed to connect to %s (rc=%d); AP restore deadline remains armed",
                    request.ssid,
                    result.returncode,
                )
            raise WifiCommandError(_command_message("connect", result))

    def disconnect(self) -> WifiStatus:
        with self._lock:
            try:
                result = self._run(
                    "nmcli",
                    ["device", "disconnect", _DEFAULT_INTERFACE],
                    timeout=_SHORT_TIMEOUT_SECONDS,
                )
                if result.returncode != 0:
                    raise WifiCommandError(_command_message("disconnect", result))
            except WifiError as exc:
                fallback = self._run(
                    "wpa_cli",
                    ["-i", _DEFAULT_INTERFACE, "disconnect"],
                    timeout=_SHORT_TIMEOUT_SECONDS,
                )
                if fallback.returncode != 0:
                    raise WifiCommandError(_command_message("disconnect", fallback)) from exc
            self.set_ap_mode(enabled=True)
            return self.get_status()

    def forget_network(self, ssid: str) -> WifiStatus:
        candidate = ssid.strip()
        if not candidate:
            raise WifiConfigError("ssid must be non-empty")
        with self._lock:
            result = self._run(
                "nmcli",
                ["connection", "delete", candidate],
                timeout=_SHORT_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                raise WifiCommandError(_command_message("forget", result))
            self._delete_credentials(candidate)
            status = self.get_status()
            if not status.connected:
                self.set_ap_mode(enabled=True)
                status = self.get_status()
            return status

    def set_ap_mode(self, *, enabled: bool) -> ApMode:
        with self._lock:
            if enabled:
                self._bring_ap_up()
                self._save_ap_state(requested_enabled=True, restore_deadline=None)
                self._schedule_restore_timer(None)
            else:
                self._bring_ap_down()
                self._save_ap_state(requested_enabled=False, restore_deadline=None)
                self._schedule_restore_timer(None)
            return self._current_ap_mode(connection_name=self._current_connection_details()[0])

    def ap_credentials_for_form(self) -> tuple[str, str]:
        """Return the current AP SSID and passphrase for pre-filling the
        captive-portal configuration form.

        Reflects any persisted operator override applied at init time.
        """
        with self._lock:
            return self._config.ap_ssid, self._config.ap_passphrase

    def update_ap_credentials(self, *, ssid: str, passphrase: str) -> ApMode:
        """Persist new AP SSID/passphrase and apply them to the live profile.

        Validates the new credentials, writes them to the AP-config
        override file so they survive restart, rebuilds the internal
        WifiConfig, and — if the AP is currently active — bounces it so
        Tesla/clients see the new credentials immediately.

        Persistence happens before any nmcli probing so that a missing
        binary or an offline NetworkManager cannot prevent the operator's
        chosen credentials from sticking across a restart.
        """
        clean_ssid = ssid.strip()
        new_config = dataclasses.replace(
            self._config,
            ap_ssid=clean_ssid,
            ap_passphrase=passphrase,
        )
        new_config.validate()
        with self._lock:
            try:
                was_active = self._current_ap_mode(
                    connection_name=self._current_connection_details()[0]
                ).active
            except WifiError as exc:
                logger.warning(
                    "Cannot probe live AP state before credential update: %s", exc
                )
                was_active = False
            self._state_store.save_ap_config(ssid=clean_ssid, passphrase=passphrase)
            self._config = new_config
            self._state_store = WifiStateStore(self._config)
            self._runner = WifiCommandRunner(self._config)
            if was_active:
                try:
                    self._bring_ap_down()
                except WifiError as exc:
                    logger.warning("Failed to bring AP down for credential rotation: %s", exc)
                self._bring_ap_up()
            try:
                return self._current_ap_mode(
                    connection_name=self._current_connection_details()[0]
                )
            except WifiError as exc:
                logger.warning(
                    "Saved AP credentials but cannot read live AP state: %s", exc
                )
                return self._current_ap_mode(connection_name=None)

    def _saved_networks_snapshot(self, *, active_ssid: str | None) -> tuple[SavedWifiNetwork, ...]:
        saved_connections = self._saved_connection_names()
        stored = self._load_credentials()
        names = sorted(saved_connections | set(stored), key=str.casefold)
        return tuple(
            SavedWifiNetwork(
                ssid=name,
                security=stored.get(name, WifiCredentials(name)).security,
                has_passphrase=bool(stored.get(name, WifiCredentials(name)).passphrase),
                active=name == active_ssid,
            )
            for name in names
            if name != self._ap_connection_name
        )

    def _current_connection_details(self) -> ConnectionDetails:
        try:
            return self._current_connection_nmcli()
        except WifiError:
            return self._current_connection_iwconfig()

    def _current_connection_nmcli(self) -> ConnectionDetails:
        return current_connection_nmcli(self._run, ap_connection_name=self._ap_connection_name)

    def _current_connection_iwconfig(self) -> ConnectionDetails:
        return current_connection_iwconfig(self._run, ap_connection_name=self._ap_connection_name)

    def _current_ap_mode(self, *, connection_name: str | None) -> ApMode:
        state = self._load_ap_state()
        restore_deadline = _parse_datetime(state.get("restore_deadline"))
        return ApMode(
            requested_enabled=bool(state.get("requested_enabled", False)),
            active=connection_name == self._ap_connection_name,
            ssid=self._config.ap_ssid,
            passphrase_configured=bool(self._config.ap_passphrase),
            restore_deadline=restore_deadline,
            state_path=self._ap_state_path,
        )

    def _security_for_ssid(self, ssid: str) -> str:
        for network in self.list_available_networks(rescan=False):
            if network.ssid == ssid:
                return network.security
        return self._load_credentials().get(ssid, WifiCredentials(ssid)).security

    def _scan_networks_nmcli(self, *, rescan: bool) -> list[NetworkRow]:
        return scan_networks_nmcli(self._run, rescan=rescan)

    def _scan_networks_iwlist(self, *, rescan: bool) -> list[NetworkRow]:
        del rescan
        return scan_networks_iwlist(self._run)

    def _saved_connection_names(self) -> set[str]:
        return saved_connection_names(self._run)

    def saved_wifi_profile_ssids(self) -> dict[str, str]:
        """Return `{profile_name: actual_ssid}` for every saved wifi profile.

        Exposed so callers (notably the saved-networks API) can
        cross-reference saved names against scan SSIDs even when the
        connection profile id differs from the broadcast SSID.
        """
        with self._lock:
            return saved_wifi_profile_ssids(self._run)

    @property
    def _ap_connection_name(self) -> str:
        return self._state_store.ap_connection_name

    @property
    def _ap_state_path(self) -> Path:
        return self._state_store.ap_state_path

    def _bring_ap_up(self) -> None:
        bring_ap_up(
            self._run,
            config=self._config,
            ap_connection_name=self._ap_connection_name,
            existing_connections=self._saved_connection_names(),
        )

    def _bring_ap_down(self) -> None:
        bring_ap_down(self._run, ap_connection_name=self._ap_connection_name)

    def _effective_passphrase(self, request: WifiConnectionRequest) -> str:
        if request.passphrase:
            return request.passphrase
        return self._load_credentials().get(request.ssid, WifiCredentials(request.ssid)).passphrase

    def _load_credentials(self) -> dict[str, WifiCredentials]:
        return self._state_store.load_credentials()

    def _store_credentials(self, credentials: WifiCredentials) -> None:
        self._state_store.store_credentials(credentials)

    def _delete_credentials(self, ssid: str) -> None:
        self._state_store.delete_credentials(ssid)

    def _load_ap_state(self) -> dict[str, object]:
        return self._state_store.load_ap_state()

    def _save_ap_state(self, *, requested_enabled: bool, restore_deadline: datetime | None) -> None:
        self._state_store.save_ap_state(
            requested_enabled=requested_enabled,
            restore_deadline=restore_deadline,
        )

    def _sorted_credentials_payload(
        self,
        stored: dict[str, WifiCredentials],
    ) -> list[dict[str, str]]:
        return self._state_store.sorted_credentials_payload(stored)

    def _write_json_file(self, path: Path, payload: object) -> None:
        self._state_store.write_json_file(path, payload)

    def _schedule_restore_timer(self, deadline: datetime | None) -> None:
        if self._restore_timer is not None:
            self._restore_timer.cancel()
            self._restore_timer = None
        if deadline is None:
            return
        seconds = max(0.0, (deadline - datetime.now(tz=UTC)).total_seconds())
        timer = threading.Timer(seconds, self._restore_ap_if_needed)
        timer.daemon = True
        self._restore_timer = timer
        timer.start()

    def _restore_ap_if_needed(self) -> None:
        with self._lock:
            self._restore_timer = None
            state = self._load_ap_state()
            restore_deadline = _parse_datetime(state.get("restore_deadline"))
            if restore_deadline is None or restore_deadline > datetime.now(tz=UTC):
                self._schedule_restore_timer(restore_deadline)
                return
            status = self.get_status()
            if status.connected:
                self._save_ap_state(requested_enabled=False, restore_deadline=None)
                return
            try:
                self._bring_ap_up()
                self._save_ap_state(requested_enabled=True, restore_deadline=None)
            except WifiError as exc:
                logger.warning("Failed to restore AP mode automatically: %s", exc)

    def _resolve_binary(self, name: str) -> Path:
        return self._runner.resolve_binary(name)

    def _run(
        self,
        binary_name: str,
        args: list[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        return self._runner.run(binary_name, args, timeout=timeout)


def make_wifi_service(cfg: WebConfig) -> WifiService:
    """Construct the shared Wi-Fi service from the typed app config."""
    return WifiService(
        WifiConfig(
            credentials_path=cfg.wifi.credentials_path,
            ap_ssid=cfg.wifi.ap_ssid,
            ap_passphrase=cfg.wifi.ap_passphrase,
            ap_idle_timeout_seconds=cfg.wifi.ap_idle_timeout_seconds,
            binary_paths=WifiBinaryPaths(
                nmcli=cfg.wifi.binary_paths.nmcli,
                iwlist=cfg.wifi.binary_paths.iwlist,
                iwconfig=cfg.wifi.binary_paths.iwconfig,
                wpa_cli=cfg.wifi.binary_paths.wpa_cli,
            ),
        )
    )
