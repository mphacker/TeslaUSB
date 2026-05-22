"""Hostapd + dnsmasq-based on-board AP for B-1.

B-1 inherits v1's concurrent AP+STA design: a virtual `wlan0_ap`
interface is added on top of `wlan0` with `iw dev wlan0 interface add
wlan0_ap type __ap`, NetworkManager is told to leave it alone, and
hostapd + dnsmasq run on it directly. This keeps the WiFi client
connection (and operator SSH over it) intact when the AP is brought
up — which the NetworkManager-managed AP profile path cannot do on a
single-radio chipset.

All privileged commands (iw, ip, hostapd, dnsmasq, pkill) are wrapped
with `sudo -n` because the Flask app runs as `pi`. A narrow allowlist
in `/etc/sudoers.d/teslausb-ap` keeps the surface tight.

The module exposes pure helpers (no shared state); the caller is the
WifiService, which holds the lock and owns lifecycle decisions.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Final

from teslausb_web.services.wifi_support import (
    WifiCommandError,
    WifiConfigError,
    WifiError,
)

logger = logging.getLogger(__name__)

# Same defaults v1 shipped with so the dashboard layout, the captive
# portal URL, and the operator's muscle memory all keep working.
AP_PHYSICAL_INTERFACE: Final[str] = "wlan0"
AP_VIRTUAL_INTERFACE: Final[str] = "wlan0_ap"
AP_GATEWAY_CIDR: Final[str] = "192.168.4.1/24"
AP_GATEWAY_IP: Final[str] = "192.168.4.1"
AP_DHCP_START: Final[str] = "192.168.4.10"
AP_DHCP_END: Final[str] = "192.168.4.50"
AP_DEFAULT_CHANNEL: Final[int] = 6

RUNTIME_DIR: Final[Path] = Path("/run/teslausb-ap")
HOSTAPD_CONF: Final[Path] = RUNTIME_DIR / "hostapd.conf"
DNSMASQ_CONF: Final[Path] = RUNTIME_DIR / "dnsmasq.conf"
HOSTAPD_PID: Final[Path] = RUNTIME_DIR / "hostapd.pid"
DNSMASQ_PID: Final[Path] = RUNTIME_DIR / "dnsmasq.pid"

_SHORT_TIMEOUT: Final[float] = 5.0
_START_TIMEOUT: Final[float] = 10.0
_PSK_MIN_LENGTH: Final[int] = 8
_PSK_MAX_LENGTH: Final[int] = 63
_SSID_MAX_LENGTH: Final[int] = 32
_MIN_CHANNEL: Final[int] = 1
_MAX_CHANNEL: Final[int] = 14

# `iw dev wlan0 link` prints lines like "freq: 2437" when associated.
# Channel 6 → 2437 MHz; the table covers the only frequencies a
# Pi Zero 2 W's BCM43436 actually supports (2.4 GHz only — the chip
# has no 5 GHz radio).
_FREQ_TO_CHANNEL: Final[dict[int, int]] = {
    2412: 1,
    2417: 2,
    2422: 3,
    2427: 4,
    2432: 5,
    2437: 6,
    2442: 7,
    2447: 8,
    2452: 9,
    2457: 10,
    2462: 11,
    2467: 12,
    2472: 13,
    2484: 14,
}


def _run_sudo(
    args: list[str],
    *,
    timeout: float = _SHORT_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a privileged command via `sudo -n`.

    Raises WifiCommandError if sudo would prompt (-n) or the command
    times out. With check=False the caller inspects returncode.
    """
    command = ["sudo", "-n", *args]
    try:
        result = subprocess.run(  # noqa: S603 - argv is fixed, no shell
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WifiCommandError(f"AP helper timed out: {' '.join(command)}") from exc
    except OSError as exc:
        raise WifiCommandError(f"Failed to execute {' '.join(command)}: {exc}") from exc
    if check and result.returncode != 0:
        raise WifiCommandError(
            f"AP helper failed ({' '.join(command)}): rc={result.returncode} "
            f"stderr={result.stderr.strip()[:200]}"
        )
    return result


def _interface_exists(name: str) -> bool:
    result = subprocess.run(  # noqa: S603 - argv is fixed
        ["ip", "-br", "link", "show", name],  # noqa: S607 - ip resolves via secure PATH
        check=False,
        capture_output=True,
        text=True,
        timeout=_SHORT_TIMEOUT,
    )
    return result.returncode == 0


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    return int(raw)


def _pid_alive(pid: int) -> bool:
    try:
        # Signal 0 = existence check, no actual signal sent.
        # Reading /proc avoids needing privileges.
        return Path(f"/proc/{pid}").exists()
    except OSError:
        return False


def is_ap_running() -> bool:
    """Return True iff both hostapd and dnsmasq are alive per their PID files.

    Matches v1's status check — file-existence alone is not enough
    because a crash can leave stale PID files.
    """
    hostapd_pid = _read_pid(HOSTAPD_PID)
    dnsmasq_pid = _read_pid(DNSMASQ_PID)
    if hostapd_pid is None or dnsmasq_pid is None:
        return False
    return _pid_alive(hostapd_pid) and _pid_alive(dnsmasq_pid)


def current_sta_channel() -> int | None:
    """Return the channel wlan0 is currently associated on, or None.

    The Pi Zero 2 W's single radio forces the AP onto the same channel
    as the STA; running them on different channels makes brcmfmac
    silently drop the AP. So we always prefer the STA's live channel.
    """
    try:
        result = subprocess.run(  # noqa: S603 - argv is fixed
            ["iw", "dev", AP_PHYSICAL_INTERFACE, "link"],  # noqa: S607 - iw resolves via secure PATH
            check=False,
            capture_output=True,
            text=True,
            timeout=_SHORT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"freq:\s*(\d+)", result.stdout)
    if not match:
        return None
    freq = int(match.group(1))
    return _FREQ_TO_CHANNEL.get(freq)


def resolve_ap_channel() -> int:
    """Pick the channel hostapd should run on (STA channel if associated)."""
    return current_sta_channel() or AP_DEFAULT_CHANNEL


def _validate_credentials(ssid: str, passphrase: str) -> None:
    if not ssid.strip():
        raise WifiConfigError("AP ssid must be non-empty")
    if len(ssid) > _SSID_MAX_LENGTH:
        raise WifiConfigError("AP ssid must be 1-32 chars")
    if not (_PSK_MIN_LENGTH <= len(passphrase) <= _PSK_MAX_LENGTH):
        raise WifiConfigError("AP passphrase must be 8-63 chars")
    # hostapd rejects non-ASCII PSKs silently.
    if not passphrase.isascii():
        raise WifiConfigError("AP passphrase must be ASCII")
    # Block characters that would let an attacker break out of the
    # hostapd.conf line.
    for ch in ("\n", "\r"):
        if ch in ssid or ch in passphrase:
            raise WifiConfigError("AP ssid/passphrase must not contain newlines")


def render_hostapd_conf(
    *, ssid: str, passphrase: str, channel: int, interface: str = AP_VIRTUAL_INTERFACE
) -> str:
    _validate_credentials(ssid, passphrase)
    if not _MIN_CHANNEL <= channel <= _MAX_CHANNEL:
        raise WifiConfigError(f"AP channel must be 1-14, got {channel}")
    return (
        f"interface={interface}\n"
        "driver=nl80211\n"
        f"ssid={ssid}\n"
        "hw_mode=g\n"
        f"channel={channel}\n"
        "wmm_enabled=0\n"
        "auth_algs=1\n"
        "wpa=2\n"
        f"wpa_passphrase={passphrase}\n"
        "wpa_key_mgmt=WPA-PSK\n"
        "rsn_pairwise=CCMP\n"
    )


def render_dnsmasq_conf(*, interface: str = AP_VIRTUAL_INTERFACE) -> str:
    # Captive-portal DNS: every name resolves to the gateway so
    # phones/laptops detect "internet probe failed → captive portal"
    # and pop the web UI automatically.
    return (
        f"interface={interface}\n"
        "bind-interfaces\n"
        f"dhcp-range={AP_DHCP_START},{AP_DHCP_END},12h\n"
        f"dhcp-option=3,{AP_GATEWAY_IP}\n"
        f"dhcp-option=6,{AP_GATEWAY_IP}\n"
        f"address=/#/{AP_GATEWAY_IP}\n"
    )


def _write_conf(path: Path, body: str) -> None:
    # /run/teslausb-ap is owned by pi (tmpfiles.d creates it on boot),
    # so we don't need sudo for the conf files themselves.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def ensure_virtual_interface() -> None:
    """Create wlan0_ap as a virtual AP interface on wlan0 if missing.

    Idempotent. Leaves wlan0 entirely alone so the existing STA
    association is preserved.
    """
    if not _interface_exists(AP_PHYSICAL_INTERFACE):
        raise WifiCommandError(
            f"Physical interface {AP_PHYSICAL_INTERFACE} not present; "
            "cannot create virtual AP interface"
        )
    if not _interface_exists(AP_VIRTUAL_INTERFACE):
        _run_sudo(
            [
                "iw",
                "dev",
                AP_PHYSICAL_INTERFACE,
                "interface",
                "add",
                AP_VIRTUAL_INTERFACE,
                "type",
                "__ap",
            ],
        )
        logger.info("Created virtual AP interface %s", AP_VIRTUAL_INTERFACE)
    # Mark unmanaged so NM doesn't try to DHCP / configure / fight us.
    _run_sudo(
        ["nmcli", "device", "set", AP_VIRTUAL_INTERFACE, "managed", "no"],
        check=False,
    )
    _run_sudo(["ip", "link", "set", AP_VIRTUAL_INTERFACE, "up"])
    # Refresh the gateway address (flush+add is idempotent).
    _run_sudo(["ip", "addr", "flush", "dev", AP_VIRTUAL_INTERFACE], check=False)
    _run_sudo(["ip", "addr", "add", AP_GATEWAY_CIDR, "dev", AP_VIRTUAL_INTERFACE])


def remove_virtual_interface() -> None:
    """Delete wlan0_ap if present. Safe to call when AP is already down."""
    if _interface_exists(AP_VIRTUAL_INTERFACE):
        _run_sudo(["ip", "addr", "flush", "dev", AP_VIRTUAL_INTERFACE], check=False)
        _run_sudo(["iw", "dev", AP_VIRTUAL_INTERFACE, "del"], check=False)


def _stop_pid(pid_path: Path) -> None:
    pid = _read_pid(pid_path)
    if pid is not None and _pid_alive(pid):
        _run_sudo(["kill", str(pid)], check=False)
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Failed to remove %s: %s", pid_path, exc)


def stop_ap() -> None:
    """Stop hostapd + dnsmasq and tear down the virtual interface."""
    _stop_pid(HOSTAPD_PID)
    _stop_pid(DNSMASQ_PID)
    remove_virtual_interface()


def start_ap(*, ssid: str, passphrase: str) -> None:
    """Bring the on-board AP up with the given credentials.

    Does not touch wlan0's STA association. If the AP is already
    running this stops it first (so credential rotation works).
    """
    _validate_credentials(ssid, passphrase)
    if is_ap_running():
        stop_ap()
    ensure_virtual_interface()
    channel = resolve_ap_channel()
    _write_conf(
        HOSTAPD_CONF,
        render_hostapd_conf(
            ssid=ssid,
            passphrase=passphrase,
            channel=channel,
        ),
    )
    _write_conf(DNSMASQ_CONF, render_dnsmasq_conf())
    # dnsmasq first so DHCP is ready the moment the first client
    # associates with hostapd.
    _run_sudo(
        ["dnsmasq", f"--conf-file={DNSMASQ_CONF}", f"--pid-file={DNSMASQ_PID}"],
        timeout=_START_TIMEOUT,
    )
    try:
        _run_sudo(
            ["hostapd", "-B", "-P", str(HOSTAPD_PID), str(HOSTAPD_CONF)],
            timeout=_START_TIMEOUT,
        )
    except WifiError:
        _stop_pid(DNSMASQ_PID)
        raise
    logger.info(
        "AP started on %s (SSID=%s, channel=%d)",
        AP_VIRTUAL_INTERFACE,
        ssid,
        channel,
    )


__all__: list[str] = [
    "AP_DEFAULT_CHANNEL",
    "AP_GATEWAY_CIDR",
    "AP_GATEWAY_IP",
    "AP_PHYSICAL_INTERFACE",
    "AP_VIRTUAL_INTERFACE",
    "DNSMASQ_CONF",
    "DNSMASQ_PID",
    "HOSTAPD_CONF",
    "HOSTAPD_PID",
    "current_sta_channel",
    "ensure_virtual_interface",
    "is_ap_running",
    "remove_virtual_interface",
    "render_dnsmasq_conf",
    "render_hostapd_conf",
    "resolve_ap_channel",
    "start_ap",
    "stop_ap",
]
