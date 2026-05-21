"""WiFi reachability checks for the cloud archive worker."""

from __future__ import annotations

import shutil
import subprocess
from typing import Final

_CONNECTED_WIFI_FIELDS: Final[int] = 3


def _is_wifi_connected() -> bool:
    nmcli_path = shutil.which("nmcli")
    if nmcli_path is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 - nmcli path is resolved via shutil.which
            [nmcli_path, "-t", "-f", "DEVICE,TYPE,STATE", "device"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in result.stdout.strip().splitlines():
        parts = line.split(":")
        if len(parts) >= _CONNECTED_WIFI_FIELDS and parts[1] == "wifi" and parts[2] == "connected":
            return True
    return False
