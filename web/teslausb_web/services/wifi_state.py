"""Wi-Fi service state persistence and command execution helpers."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

from teslausb_web.services.wifi_support import (
    _FILE_MODE_PRIVATE,
    WifiCommandError,
    WifiConfig,
    WifiConfigError,
    WifiCredentials,
    WifiError,
)


class WifiStateStore:
    def __init__(self, config: WifiConfig) -> None:
        self._config = config

    @property
    def ap_connection_name(self) -> str:
        return f"{self._config.ap_ssid} AP"

    @property
    def ap_state_path(self) -> Path:
        return self._config.credentials_path.with_name(
            f"{self._config.credentials_path.stem}_ap_state.json"
        )

    def load_credentials(self) -> dict[str, WifiCredentials]:
        path = self._config.credentials_path
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise WifiError(f"Failed to read Wi-Fi credentials store: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise WifiConfigError(f"Invalid Wi-Fi credentials JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise WifiConfigError("Wi-Fi credentials store must contain a JSON array")
        credentials: dict[str, WifiCredentials] = {}
        for item in payload:
            if not isinstance(item, dict):
                raise WifiConfigError("Wi-Fi credentials entries must be JSON objects")
            ssid = str(item.get("ssid", "")).strip()
            if not ssid:
                continue
            credentials[ssid] = WifiCredentials(
                ssid=ssid,
                passphrase=str(item.get("passphrase", "")),
                security=str(item.get("security", "")),
            )
        return credentials

    def store_credentials(self, credentials: WifiCredentials) -> None:
        stored = self.load_credentials()
        stored[credentials.ssid] = credentials
        self.write_json_file(
            self._config.credentials_path,
            self.sorted_credentials_payload(stored),
        )

    def delete_credentials(self, ssid: str) -> None:
        stored = self.load_credentials()
        if ssid in stored:
            del stored[ssid]
            self.write_json_file(
                self._config.credentials_path,
                self.sorted_credentials_payload(stored),
            )

    def load_ap_state(self) -> dict[str, object]:
        if not self.ap_state_path.exists():
            return {"requested_enabled": False, "restore_deadline": None}
        try:
            payload = json.loads(self.ap_state_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise WifiError(f"Failed to read AP state file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise WifiConfigError(f"Invalid AP state JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise WifiConfigError("AP state file must contain a JSON object")
        return payload

    def save_ap_state(
        self,
        *,
        requested_enabled: bool,
        restore_deadline: datetime | None,
    ) -> None:
        payload = {
            "requested_enabled": requested_enabled,
            "restore_deadline": restore_deadline.isoformat() if restore_deadline else None,
        }
        self.write_json_file(self.ap_state_path, payload)

    def sorted_credentials_payload(
        self,
        stored: dict[str, WifiCredentials],
    ) -> list[dict[str, str]]:
        return [
            asdict(entry)
            for entry in sorted(stored.values(), key=lambda item: item.ssid.casefold())
        ]

    def write_json_file(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f"{path.stem}-",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            temp_path = Path(temp_name)
            if os.name == "posix":
                temp_path.chmod(_FILE_MODE_PRIVATE)
            temp_path.replace(path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                Path(temp_name).unlink()
            raise WifiError(f"Failed to write {path}: {exc}") from exc


class WifiCommandRunner:
    def __init__(self, config: WifiConfig) -> None:
        self._config = config

    def resolve_binary(self, name: str) -> Path:
        configured = getattr(self._config.binary_paths, name)
        resolved = shutil.which(configured)
        if resolved is None:
            raise WifiError(
                "Required Wi-Fi binary "
                f"{configured!r} was not found in PATH; "
                f"configure [wifi.binary_paths].{name}"
            )
        return Path(resolved)

    def run(
        self,
        binary_name: str,
        args: list[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(self.resolve_binary(binary_name)), *args]
        try:
            return subprocess.run(  # noqa: S603 - executable path comes from shutil.which resolution above.
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise WifiCommandError(f"Wi-Fi command timed out: {' '.join(command)}") from exc
        except OSError as exc:
            raise WifiCommandError(f"Failed to execute {' '.join(command)}: {exc}") from exc
