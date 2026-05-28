"""B-1 service: storage + parsing for generic (non-OAuth) rclone remotes.

The Cloud Archive UI exposes a "Connect" flow for S3, Backblaze B2,
Wasabi, generic-SFTP, WebDAV, SMB, FTP, Azure Blob, and OpenStack Swift.
These are NOT OAuth providers — they're plain rclone backends configured
with a static set of keys (``host`` / ``user`` / ``pass`` for sftp;
``access_key_id`` / ``secret_access_key`` for S3-family; etc.).

This module mirrors the OAuth service (:mod:`cloud_oauth_service`) but
stores the connection record as a plain JSON blob at
``cloud_generic_remote.json`` (sibling of the OAuth credentials file).
The on-disk schema is::

    {"type": "sftp", "host": "nas.local", "user": "pi", "pass": "obscured",
     "_obscure_keys": "pass", "_source": "form"}

Keys starting with ``_`` are storage metadata; everything else is a
literal rclone.conf key.  :func:`load` returns the raw dict (so the
rclone-conf renderer in :mod:`cloud_rclone_service` can iterate it
without knowing which fields are required).

The port follows v1 (``scripts/web/services/cloud_rclone_service.py``)
verbatim for the security-critical bits: rclone-config-injection
defenses (control-char rejection on keys, values, and source labels),
single-section enforcement (to block ``crypt``-wrap smuggling), and
``rclone obscure`` subprocess delegation for password fields.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


_GENERIC_RCLONE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "sftp",
        "webdav",
        "smb",
        "ftp",
        "s3",
        "b2",
        "wasabi",
        "azureblob",
        "swift",
    }
)

# Default ``obscure_keys`` per backend. Mirrors v1.
_DEFAULT_OBSCURE_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "sftp": ("pass",),
    "webdav": ("pass",),
    "smb": ("pass",),
    "ftp": ("pass",),
    "s3": (),
    "b2": (),
    "wasabi": (),
    "azureblob": (),
    "swift": (),
}
assert set(_DEFAULT_OBSCURE_KEYS.keys()) == set(_GENERIC_RCLONE_TYPES), (
    "_DEFAULT_OBSCURE_KEYS must cover every backend in _GENERIC_RCLONE_TYPES"
)

# Storage-metadata keys (prefixed ``_``) that the rclone-conf renderer
# MUST skip when iterating the loaded dict — they would otherwise be
# written into the conf file as bogus rclone parameters.
_META_KEYS: Final[tuple[str, ...]] = ("_obscure_keys", "_source")

# Characters that cannot appear in any field key or value — newline /
# carriage-return / NUL would let an attacker inject extra config lines
# into the ``[teslausb]`` block. Ported verbatim from v1; see the PR
# #218 review for the threat model (sftp ``ssh`` directive → RCE,
# s3 ``endpoint`` override → silent upload redirection).
_FORBIDDEN_FIELD_CHARS: Final[tuple[str, ...]] = ("\n", "\r", "\x00")


class GenericRemoteError(Exception):
    """Raised for any validation or storage failure in this module."""


def supported_types() -> tuple[str, ...]:
    """Return the sorted tuple of accepted ``rclone_type`` values."""
    return tuple(sorted(_GENERIC_RCLONE_TYPES))


def default_obscure_keys(rclone_type: str) -> tuple[str, ...]:
    """Return the default ``obscure_keys`` list for a backend."""
    return _DEFAULT_OBSCURE_KEYS.get(rclone_type, ())


def _reject_control_chars(label: str, value: str) -> None:
    for char in _FORBIDDEN_FIELD_CHARS:
        if char in value:
            raise GenericRemoteError(
                f"{label} contains a forbidden control character "
                f"(0x{ord(char):02x}); rclone config injection is blocked here."
            )


def _rclone_obscure(plaintext: str, *, rclone_binary: str = "rclone") -> str:
    """Return ``rclone obscure <plaintext>`` for use in the conf file.

    Delegates to the rclone binary so we never have to re-implement its
    KDF. Raises :class:`GenericRemoteError` on any failure — never
    silently returns the cleartext (that would leave a real password in
    ``rclone.conf``).
    """
    if not isinstance(plaintext, str):
        raise GenericRemoteError("rclone obscure: value must be a string")
    if plaintext == "":
        return ""
    try:
        result = subprocess.run(  # noqa: S603 - argv is fully literal
            [rclone_binary, "obscure", plaintext],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GenericRemoteError("rclone binary not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GenericRemoteError("rclone obscure timed out") from exc
    if result.returncode != 0:
        raise GenericRemoteError(
            f"rclone obscure failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()[:200]}"
        )
    obscured = (result.stdout or "").strip()
    if not obscured:
        raise GenericRemoteError("rclone obscure returned empty output")
    return obscured


def parse_config_block(text: str) -> dict[str, str]:
    """Parse a pasted ``rclone.conf`` block into a flat ``{key: value}`` dict.

    Accepts either the section-header form (``[my-nas]\\ntype = sftp\\n...``)
    or a bare key=value list. The section name is discarded — the caller
    decides the ultimate remote name. Multiple sections are rejected.
    """
    if not isinstance(text, str):
        raise GenericRemoteError("rclone config block must be a string")
    section_count = 0
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_count += 1
            if section_count > 1:
                raise GenericRemoteError(
                    "rclone config block must contain at most one [section]; "
                    "wrap remotes (crypt/union/chunker) are not supported"
                )
            continue
        if "=" not in line:
            raise GenericRemoteError(f"invalid rclone config line: {line!r}")
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not key:
            raise GenericRemoteError(f"invalid rclone config line: {line!r}")
        out[key] = value
    if "type" not in out:
        raise GenericRemoteError("rclone config block missing required 'type' key")
    if out["type"] not in _GENERIC_RCLONE_TYPES:
        raise GenericRemoteError(
            f"rclone backend type {out['type']!r} is not in the supported "
            f"set: {sorted(_GENERIC_RCLONE_TYPES)}"
        )
    return out


class GenericRemoteService:
    """Atomic JSON-file storage for a single generic rclone remote.

    Only one remote is stored at a time (the device has one cloud
    destination). Saving a second remote overwrites the first.
    """

    def __init__(self, storage_path: Path, *, rclone_binary: str = "rclone") -> None:
        self._path = storage_path
        self._rclone_binary = rclone_binary
        self._lock = threading.Lock()

    @property
    def storage_path(self) -> Path:
        return self._path

    def load(self) -> dict[str, str] | None:
        """Return the stored remote dict, or ``None`` if nothing is saved.

        I/O errors are logged and swallowed (return ``None``) so that a
        corrupt file does not crash the whole cloud page; the operator
        can re-connect to overwrite it.
        """
        with self._lock:
            if not self._path.exists():
                return None
            try:
                raw = self._path.read_text(encoding="utf-8")
                payload = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("generic-remote storage unreadable at %s: %s", self._path, exc)
                return None
        if not isinstance(payload, dict):
            return None
        # Coerce all values to str — the conf renderer requires strings.
        return {str(k): "" if v is None else str(v) for k, v in payload.items()}

    def clear(self) -> bool:
        """Delete the stored remote. Returns True if a file was removed."""
        with self._lock:
            try:
                self._path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise GenericRemoteError(
                    f"could not remove generic-remote storage: {exc}"
                ) from exc
        logger.info("Cleared generic rclone remote at %s", self._path)
        return True

    def import_form(
        self,
        rclone_type: str,
        fields: dict[str, object],
        *,
        obscure_keys: list[str] | tuple[str, ...] | None = None,
        source: str = "form",
    ) -> dict[str, str]:
        """Persist a generic remote from a form-style payload.

        ``obscure_keys`` defaults to :func:`default_obscure_keys` for
        the chosen backend when ``None`` is passed. Pass an explicit
        empty list to disable obscuring entirely (e.g. if the operator
        has already pre-obscured the value).
        """
        if rclone_type not in _GENERIC_RCLONE_TYPES:
            raise GenericRemoteError(
                f"rclone backend type {rclone_type!r} is not in the supported "
                f"set: {sorted(_GENERIC_RCLONE_TYPES)}"
            )
        if not isinstance(fields, dict):
            raise GenericRemoteError("fields must be a dict")
        if obscure_keys is None:
            obscure_list: list[str] = list(default_obscure_keys(rclone_type))
        elif isinstance(obscure_keys, (list, tuple)):
            obscure_list = []
            for entry in obscure_keys:
                if not isinstance(entry, str):
                    raise GenericRemoteError("obscure_keys entries must be strings")
                obscure_list.append(entry.strip().lower())
        else:
            raise GenericRemoteError(
                "obscure_keys must be a list of strings, not "
                f"{type(obscure_keys).__name__}"
            )

        record: dict[str, str] = {"type": rclone_type}
        for raw_key, raw_value in fields.items():
            if not isinstance(raw_key, str):
                raise GenericRemoteError("field keys must be strings")
            key = raw_key.strip().lower()
            if not key:
                raise GenericRemoteError("field keys must be non-empty")
            if key.startswith("_"):
                raise GenericRemoteError(
                    f"field key {raw_key!r} is reserved (leading underscore)"
                )
            if key == "type":
                raise GenericRemoteError(
                    "'type' is set from rclone_type; remove it from fields"
                )
            _reject_control_chars(f"field key {raw_key!r}", key)
            value = "" if raw_value is None else str(raw_value)
            _reject_control_chars(f"value for {key!r}", value)
            if key in obscure_list:
                value = _rclone_obscure(value, rclone_binary=self._rclone_binary)
                _reject_control_chars(f"obscured value for {key!r}", value)
            record[key] = value

        _reject_control_chars("source", source)
        record["_obscure_keys"] = ",".join(sorted(set(obscure_list)))
        record["_source"] = source
        self._write(record)
        logger.info("Imported generic rclone remote (type=%s, source=%s)", rclone_type, source)
        return record

    def import_config_block(self, text: str, *, source: str = "paste") -> dict[str, str]:
        """Persist a generic remote from a pasted rclone.conf block.

        Parses ``text`` via :func:`parse_config_block`, peels off the
        ``type`` to use as ``rclone_type``, and routes the rest through
        :meth:`import_form` with the default obscure-keys for that
        backend. Values that are already obscured (typical when the
        operator pastes their existing rclone.conf) are re-obscured —
        rclone tolerates double-obscure for sftp/webdav/smb/ftp but
        operators who want to preserve a hand-set obscured value should
        use the form flow with an explicit empty ``obscure_keys``.
        """
        parsed = parse_config_block(text)
        rclone_type = parsed.pop("type")
        # Drop any stale obscure metadata that might have slipped into
        # the pasted block — we re-derive it from the backend defaults.
        for meta_key in _META_KEYS:
            parsed.pop(meta_key, None)
        # When pasting an existing rclone.conf the password field is
        # already obscured; double-obscuring would silently break login.
        # The safe behaviour is to skip obscuring on the paste path —
        # the operator vouches for what they pasted.
        return self.import_form(
            rclone_type,
            {key: value for key, value in parsed.items()},
            obscure_keys=[],
            source=source,
        )

    def _write(self, record: dict[str, str]) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(record, sort_keys=True, indent=2)
        with self._lock:
            fd, tmp_name = tempfile.mkstemp(
                prefix=self._path.name + ".",
                suffix=".tmp",
                dir=str(directory),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self._path)
            except OSError:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            if os.name != "nt":
                try:
                    os.chmod(self._path, 0o600)
                except OSError as exc:
                    logger.debug("could not chmod generic-remote storage: %s", exc)


def render_conf_body(record: dict[str, str], *, remote_name: str = "teslausb") -> str:
    """Render a stored generic-remote record as an rclone.conf body.

    The output starts with ``[<remote_name>]`` and contains one
    ``key = value`` line per non-meta field. Metadata keys (``_obscure_keys``,
    ``_source``) are skipped. ``type`` is emitted first for readability.
    """
    if "type" not in record:
        raise GenericRemoteError("generic-remote record missing 'type'")
    lines = [f"[{remote_name}]", f"type = {record['type']}"]
    for key in sorted(record.keys()):
        if key == "type" or key in _META_KEYS:
            continue
        lines.append(f"{key} = {record[key]}")
    return "\n".join(lines) + "\n"


def make_generic_remote_service(cfg) -> GenericRemoteService:  # type: ignore[no-untyped-def]
    """Build a :class:`GenericRemoteService` rooted next to OAuth creds."""
    oauth_path = cfg.cloud.credentials_path
    storage_path = oauth_path.with_name("cloud_generic_remote.json")
    rclone_binary = cfg.cloud.rclone_binary
    return GenericRemoteService(storage_path, rclone_binary=rclone_binary)


__all__ = (
    "GenericRemoteError",
    "GenericRemoteService",
    "default_obscure_keys",
    "make_generic_remote_service",
    "parse_config_block",
    "render_conf_body",
    "supported_types",
)
