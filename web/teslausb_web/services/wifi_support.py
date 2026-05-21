"""Shared Wi-Fi service support types and helpers."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Protocol, TypedDict

if TYPE_CHECKING:
    import subprocess

_DEFAULT_INTERFACE: Final[str] = "wlan0"
_SHORT_TIMEOUT_SECONDS: Final[float] = 5.0
_SCAN_TIMEOUT_SECONDS: Final[float] = 20.0
_CONNECT_TIMEOUT_SECONDS: Final[float] = 45.0
_FILE_MODE_PRIVATE: Final[int] = 0o600
_SSID_MAX_LENGTH: Final[int] = 32
_PASSPHRASE_MIN_LENGTH: Final[int] = 8
_PASSPHRASE_MAX_LENGTH: Final[int] = 63
_NMCLI_DEVICE_STATUS_FIELD_COUNT: Final[int] = 4
_NMCLI_ACTIVE_SCAN_FIELD_COUNT: Final[int] = 3
_NMCLI_SCAN_FIELD_COUNT: Final[int] = 4
_NMCLI_CONNECTION_FIELD_COUNT: Final[int] = 2
_NMCLI_INACTIVE_CONNECTION_RETURNCODE: Final[int] = 10

ConnectionDetails = tuple[str | None, str | None, int | None, str | None, bool]


class NetworkRow(TypedDict):
    ssid: str
    signal_strength: int | None
    secured: bool
    security: str
    active: bool


class WifiError(RuntimeError):
    """Base error for Wi-Fi service failures."""


class WifiCommandError(WifiError):
    """Raised when a Wi-Fi subprocess fails or times out."""


class WifiConfigError(ValueError):
    """Raised when the Wi-Fi service configuration is invalid."""


@dataclass(frozen=True, slots=True)
class WifiBinaryPaths:
    nmcli: str = "nmcli"
    iwlist: str = "iwlist"
    iwconfig: str = "iwconfig"
    wpa_cli: str = "wpa_cli"

    def validate(self) -> None:
        for field_name, value in asdict(self).items():
            if not value.strip():
                raise WifiConfigError(f"binary_paths.{field_name} must be non-empty")


@dataclass(frozen=True, slots=True)
class WifiConfig:
    credentials_path: Path
    ap_ssid: str = "TeslaUSB-Setup"
    ap_passphrase: str = ""
    ap_idle_timeout_seconds: int = 600
    binary_paths: WifiBinaryPaths = field(default_factory=WifiBinaryPaths)

    def validate(self) -> None:
        self.binary_paths.validate()
        if (
            not self.credentials_path.is_absolute()
            and not PurePosixPath(self.credentials_path.as_posix()).is_absolute()
        ):
            raise WifiConfigError(
                f"credentials_path must be absolute, got {self.credentials_path!r}"
            )
        if not self.ap_ssid.strip():
            raise WifiConfigError("ap_ssid must be non-empty")
        if self.ap_passphrase and not (
            _PASSPHRASE_MIN_LENGTH <= len(self.ap_passphrase) <= _PASSPHRASE_MAX_LENGTH
        ):
            raise WifiConfigError("ap_passphrase must be 8-63 characters or empty")
        if self.ap_idle_timeout_seconds <= 0:
            raise WifiConfigError("ap_idle_timeout_seconds must be > 0")


@dataclass(frozen=True, slots=True)
class WifiCredentials:
    ssid: str
    passphrase: str = ""
    security: str = ""


@dataclass(frozen=True, slots=True)
class WifiConnectionRequest:
    ssid: str
    passphrase: str = ""

    def validate(self) -> None:
        candidate = self.ssid.strip()
        if not candidate or len(candidate) > _SSID_MAX_LENGTH:
            raise WifiConfigError("ssid must be 1-32 characters")
        if self.passphrase and not (
            _PASSPHRASE_MIN_LENGTH <= len(self.passphrase) <= _PASSPHRASE_MAX_LENGTH
        ):
            raise WifiConfigError("passphrase must be 8-63 characters or empty")


@dataclass(frozen=True, slots=True)
class SavedWifiNetwork:
    ssid: str
    security: str
    has_passphrase: bool
    active: bool


@dataclass(frozen=True, slots=True)
class WifiNetwork:
    ssid: str
    signal_strength: int | None
    secured: bool
    security: str
    active: bool
    saved: bool


@dataclass(frozen=True, slots=True)
class ApMode:
    requested_enabled: bool
    active: bool
    ssid: str
    passphrase_configured: bool
    restore_deadline: datetime | None
    state_path: Path


@dataclass(frozen=True, slots=True)
class WifiStatus:
    connected: bool
    current_ssid: str | None
    signal_strength: int | None
    ip_address: str | None
    ap_mode: ApMode
    saved_networks: tuple[SavedWifiNetwork, ...]


def _command_message(action: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout).strip()
    if not detail:
        detail = f"exit code {result.returncode}"
    return f"Wi-Fi {action} failed: {detail}"


def _nmcli_value(value: str) -> str | None:
    candidate = value.strip()
    return None if candidate in {"", "--"} else candidate


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _split_nmcli_fields(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == ":":
            fields.append("".join(current))
            current.clear()
            continue
        current.append(character)
    fields.append("".join(current))
    return fields


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return int(candidate)
    except ValueError:
        return None


class RunCommand(Protocol):
    def __call__(
        self,
        binary_name: str,
        args: list[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def current_connection_nmcli(run: RunCommand, *, ap_connection_name: str) -> ConnectionDetails:
    device_status = run(
        "nmcli",
        ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
        timeout=_SHORT_TIMEOUT_SECONDS,
    )
    if device_status.returncode != 0:
        raise WifiCommandError(_command_message("status", device_status))
    connection_name: str | None = None
    device_state = ""
    for line in device_status.stdout.splitlines():
        fields = _split_nmcli_fields(line)
        if (
            len(fields) >= _NMCLI_DEVICE_STATUS_FIELD_COUNT
            and fields[0] == _DEFAULT_INTERFACE
            and fields[1] == "wifi"
        ):
            device_state = fields[2]
            connection_name = _nmcli_value(fields[3])
            break
    device_show = run(
        "nmcli",
        ["-t", "-f", "GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", _DEFAULT_INTERFACE],
        timeout=_SHORT_TIMEOUT_SECONDS,
    )
    if device_show.returncode != 0:
        raise WifiCommandError(_command_message("status", device_show))
    ip_address: str | None = None
    for line in device_show.stdout.splitlines():
        if line.startswith("IP4.ADDRESS") and ":" in line:
            ip_address = line.split(":", 1)[1].split("/", 1)[0].strip()
            break
    signal_strength: int | None = None
    scan = run(
        "nmcli",
        ["-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi", "list", "--rescan", "no"],
        timeout=_SHORT_TIMEOUT_SECONDS,
    )
    if scan.returncode == 0:
        for line in scan.stdout.splitlines():
            fields = _split_nmcli_fields(line)
            if len(fields) >= _NMCLI_ACTIVE_SCAN_FIELD_COUNT and fields[0] == "yes":
                signal_strength = _to_int(fields[2])
                if connection_name is None:
                    connection_name = _nmcli_value(fields[1])
                break
    connected = (
        bool(connection_name)
        and connection_name != ap_connection_name
        and device_state == "connected"
    )
    return connection_name, connection_name, signal_strength, ip_address, connected


def current_connection_iwconfig(run: RunCommand, *, ap_connection_name: str) -> ConnectionDetails:
    result = run("iwconfig", [_DEFAULT_INTERFACE], timeout=_SHORT_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise WifiCommandError(_command_message("status", result))
    ssid_match = re.search(r'ESSID:"(?P<ssid>[^"]*)"', result.stdout)
    signal_match = re.search(r"Signal level=(?P<signal>-?\d+)", result.stdout)
    ssid = ssid_match.group("ssid") if ssid_match else None
    signal_strength = _to_int(signal_match.group("signal")) if signal_match else None
    connected = bool(ssid and ssid != "off/any")
    if ssid == ap_connection_name:
        connected = False
    return ssid, ssid, signal_strength, None, connected


def scan_networks_nmcli(run: RunCommand, *, rescan: bool) -> list[NetworkRow]:
    if rescan:
        rescan_result = run(
            "nmcli",
            ["device", "wifi", "rescan"],
            timeout=_SCAN_TIMEOUT_SECONDS,
        )
        if rescan_result.returncode != 0:
            raise WifiCommandError(_command_message("rescan", rescan_result))
    result = run(
        "nmcli",
        ["-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "no"],
        timeout=_SCAN_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise WifiCommandError(_command_message("scan", result))
    networks: dict[str, NetworkRow] = {}
    for line in result.stdout.splitlines():
        fields = _split_nmcli_fields(line)
        if len(fields) < _NMCLI_SCAN_FIELD_COUNT:
            continue
        ssid = fields[1].strip()
        if not ssid:
            continue
        signal = _to_int(fields[2])
        row: NetworkRow = {
            "ssid": ssid,
            "signal_strength": signal,
            "secured": bool(fields[3].strip()),
            "security": fields[3].strip() or "open",
            "active": fields[0] == "yes",
        }
        existing = networks.get(ssid)
        if existing is None or (signal or -1) > (existing["signal_strength"] or -1):
            networks[ssid] = row
    return sorted(
        networks.values(),
        key=lambda row: int(row["signal_strength"] or 0),
        reverse=True,
    )


def scan_networks_iwlist(run: RunCommand) -> list[NetworkRow]:
    result = run("iwlist", [_DEFAULT_INTERFACE, "scan"], timeout=_SCAN_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise WifiCommandError(_command_message("scan", result))
    cells = re.split(r"\n\s*Cell \d+ - ", result.stdout)
    networks: dict[str, NetworkRow] = {}
    for cell in cells:
        ssid_match = re.search(r'ESSID:"(?P<ssid>[^"]*)"', cell)
        if ssid_match is None:
            continue
        ssid = ssid_match.group("ssid").strip()
        if not ssid:
            continue
        quality_match = re.search(r"Quality=(?P<value>\d+)/(\d+)", cell)
        signal_strength = _to_int(quality_match.group("value")) if quality_match else None
        secured = "Encryption key:on" in cell
        row: NetworkRow = {
            "ssid": ssid,
            "signal_strength": signal_strength,
            "secured": secured,
            "security": "secured" if secured else "open",
            "active": False,
        }
        existing = networks.get(ssid)
        if existing is None or (signal_strength or -1) > (existing["signal_strength"] or -1):
            networks[ssid] = row
    return sorted(
        networks.values(),
        key=lambda row: int(row["signal_strength"] or 0),
        reverse=True,
    )


def saved_connection_names(run: RunCommand) -> set[str]:
    result = run(
        "nmcli",
        ["-t", "-f", "NAME,TYPE", "connection", "show"],
        timeout=_SHORT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise WifiCommandError(_command_message("list saved connections", result))
    return {
        fields[0]
        for line in result.stdout.splitlines()
        if (fields := _split_nmcli_fields(line))
        and len(fields) >= _NMCLI_CONNECTION_FIELD_COUNT
        and fields[1] == "802-11-wireless"
    }


def bring_ap_up(
    run: RunCommand,
    *,
    config: WifiConfig,
    ap_connection_name: str,
    existing_connections: set[str],
) -> None:
    if ap_connection_name not in existing_connections:
        create = run(
            "nmcli",
            [
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                _DEFAULT_INTERFACE,
                "con-name",
                ap_connection_name,
                "ssid",
                config.ap_ssid,
            ],
            timeout=_SHORT_TIMEOUT_SECONDS,
        )
        if create.returncode != 0:
            raise WifiCommandError(_command_message("create AP profile", create))
    modify = [
        "connection",
        "modify",
        ap_connection_name,
        "802-11-wireless.mode",
        "ap",
        "connection.autoconnect",
        "no",
        "ipv4.method",
        "shared",
        "ipv6.method",
        "ignore",
    ]
    if config.ap_passphrase:
        modify.extend(
            [
                "wifi-sec.key-mgmt",
                "wpa-psk",
                "wifi-sec.psk",
                config.ap_passphrase,
            ]
        )
    else:
        modify.extend(["wifi-sec.key-mgmt", "none"])
    update = run("nmcli", modify, timeout=_SHORT_TIMEOUT_SECONDS)
    if update.returncode != 0:
        raise WifiCommandError(_command_message("configure AP profile", update))
    up = run(
        "nmcli",
        ["connection", "up", ap_connection_name, "ifname", _DEFAULT_INTERFACE],
        timeout=_CONNECT_TIMEOUT_SECONDS,
    )
    if up.returncode != 0:
        raise WifiCommandError(_command_message("enable AP mode", up))


def bring_ap_down(run: RunCommand, *, ap_connection_name: str) -> None:
    down = run(
        "nmcli",
        ["connection", "down", ap_connection_name],
        timeout=_SHORT_TIMEOUT_SECONDS,
    )
    if down.returncode not in {0, _NMCLI_INACTIVE_CONNECTION_RETURNCODE}:
        raise WifiCommandError(_command_message("disable AP mode", down))
