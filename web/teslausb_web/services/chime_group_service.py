"""B-1 service: chime-group persistence and random selection.

This module ports v1's chime-group JSON store into the charter-compliant
service layer. It is safe for concurrent use inside a single Python process
because all mutating operations take an in-process ``threading.Lock`` before
reading or writing state.

That lock does **not** coordinate across Gunicorn workers or other processes.
Deployments that use this service must therefore run Gunicorn with ``--workers 1``
or add external file/distributed coordination before enabling concurrent writers.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_GROUP_NAME_MAX_LEN: Final[int] = 100
_GROUP_NAME_MIN_LEN: Final[int] = 1
_MAX_GROUPS: Final[int] = 50
_ISO_FMT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"
_GROUPS_JSON_INDENT: Final[int] = 2
_JSON_ENCODING: Final[str] = "utf-8"
_TMP_SUFFIX: Final[str] = ".tmp"
_LEGACY_CHIMES_KEY: Final[str] = "chimes"
_GROUP_CHIMES_KEY: Final[str] = "chime_filenames"
_RANDOM_CONFIG_ENABLED_KEY: Final[str] = "enabled"
_RANDOM_CONFIG_GROUP_ID_KEY: Final[str] = "group_id"
_RANDOM_CONFIG_LAST_SELECTED_KEY: Final[str] = "last_selected"
_RANDOM_CONFIG_LAST_SELECTED_AT_KEY: Final[str] = "last_selected_at"


class ChimeGroupError(ValueError):
    """The requested group operation is invalid."""


class ChimeGroupStateError(OSError):
    """The group or random-selection JSON state could not be read or written."""


@dataclass(frozen=True, slots=True)
class ChimeGroup:
    """Immutable view of a persisted chime group."""

    id: str
    name: str
    chime_filenames: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RandomConfig:
    """Immutable view of random-selection state."""

    enabled: bool
    group_id: str | None
    last_selected: str | None
    last_selected_at: datetime | None


@dataclass(frozen=True, slots=True)
class GroupOperationResult:
    """Structured result for create/rename/delete operations."""

    ok: bool
    message: str
    group_id: str | None


_DEFAULT_RANDOM_CONFIG: Final[RandomConfig] = RandomConfig(
    enabled=False,
    group_id=None,
    last_selected=None,
    last_selected_at=None,
)


class ChimeGroupManager:
    """Manage chime groups and random-selection state backed by JSON files."""

    def __init__(self, groups_file: Path, random_config_file: Path) -> None:
        self._groups_file = groups_file
        self._random_config_file = random_config_file
        self._lock = threading.Lock()
        self._groups = self._load_groups_or_empty()
        self._random_config = self._load_random_config_or_default()

    def list_groups(self) -> tuple[ChimeGroup, ...]:
        """Return all groups sorted case-insensitively by name."""
        with self._lock:
            return tuple(sorted(self._groups.values(), key=lambda group: group.name.lower()))

    def get_group(self, group_id: str) -> ChimeGroup | None:
        """Return one group, or ``None`` when the ID is unknown."""
        with self._lock:
            return self._groups.get(group_id)

    def create_group(
        self,
        name: str,
        *,
        chime_filenames: Sequence[str] = (),
    ) -> GroupOperationResult:
        """Create and persist a new group."""
        normalized_name = _normalize_group_name(name)
        normalized_chimes = _normalize_chime_filenames(chime_filenames)
        with self._lock:
            if len(self._groups) >= _MAX_GROUPS:
                msg = f"Cannot create more than {_MAX_GROUPS} groups"
                raise ChimeGroupError(msg)
            _ensure_unique_group_name(self._groups, normalized_name)
            group_id = str(uuid.uuid4())
            timestamp = _utc_now()
            new_group = ChimeGroup(
                id=group_id,
                name=normalized_name,
                chime_filenames=normalized_chimes,
                created_at=timestamp,
                updated_at=timestamp,
            )
            new_groups = dict(self._groups)
            new_groups[group_id] = new_group
            self._persist_groups(new_groups)
            self._groups = new_groups
        return GroupOperationResult(
            ok=True,
            message=f"Group '{normalized_name}' created",
            group_id=group_id,
        )

    def rename_group(self, group_id: str, new_name: str) -> GroupOperationResult:
        """Rename an existing group."""
        normalized_name = _normalize_group_name(new_name)
        with self._lock:
            group = _require_group(self._groups, group_id)
            _ensure_unique_group_name(self._groups, normalized_name, exclude_group_id=group_id)
            renamed_group = ChimeGroup(
                id=group.id,
                name=normalized_name,
                chime_filenames=group.chime_filenames,
                created_at=group.created_at,
                updated_at=_utc_now(),
            )
            new_groups = dict(self._groups)
            new_groups[group_id] = renamed_group
            self._persist_groups(new_groups)
            self._groups = new_groups
        return GroupOperationResult(
            ok=True,
            message=f"Group '{normalized_name}' renamed",
            group_id=group_id,
        )

    def delete_group(self, group_id: str) -> GroupOperationResult:
        """Delete one group unless random mode currently depends on it."""
        with self._lock:
            group = _require_group(self._groups, group_id)
            if self._random_config.enabled and self._random_config.group_id == group_id:
                msg = "Cannot delete a group while random mode is enabled for it"
                raise ChimeGroupError(msg)
            new_groups = dict(self._groups)
            del new_groups[group_id]
            self._persist_groups(new_groups)
            self._groups = new_groups
        return GroupOperationResult(
            ok=True,
            message=f"Group '{group.name}' deleted",
            group_id=group_id,
        )

    def add_chime_to_group(self, group_id: str, chime_filename: str) -> ChimeGroup:
        """Append one chime filename to a group."""
        normalized_filename = _normalize_chime_filename(chime_filename)
        with self._lock:
            group = _require_group(self._groups, group_id)
            if normalized_filename in group.chime_filenames:
                msg = f"Chime '{normalized_filename}' already exists in group '{group.name}'"
                raise ChimeGroupError(msg)
            updated_group = ChimeGroup(
                id=group.id,
                name=group.name,
                chime_filenames=(*group.chime_filenames, normalized_filename),
                created_at=group.created_at,
                updated_at=_utc_now(),
            )
            new_groups = dict(self._groups)
            new_groups[group_id] = updated_group
            self._persist_groups(new_groups)
            self._groups = new_groups
            return updated_group

    def remove_chime_from_group(self, group_id: str, chime_filename: str) -> ChimeGroup:
        """Remove one chime filename from a group."""
        normalized_filename = _normalize_chime_filename(chime_filename)
        with self._lock:
            group = _require_group(self._groups, group_id)
            if normalized_filename not in group.chime_filenames:
                msg = f"Chime '{normalized_filename}' is not in group '{group.name}'"
                raise ChimeGroupError(msg)
            updated_group = ChimeGroup(
                id=group.id,
                name=group.name,
                chime_filenames=tuple(
                    filename
                    for filename in group.chime_filenames
                    if filename != normalized_filename
                ),
                created_at=group.created_at,
                updated_at=_utc_now(),
            )
            new_groups = dict(self._groups)
            new_groups[group_id] = updated_group
            self._persist_groups(new_groups)
            self._groups = new_groups
            return updated_group

    def list_group_chimes(self, group_id: str) -> tuple[str, ...]:
        """Return the filenames assigned to one group."""
        with self._lock:
            return _require_group(self._groups, group_id).chime_filenames

    def get_random_config(self) -> RandomConfig:
        """Return the current random-selection configuration."""
        with self._lock:
            return self._random_config

    def set_random_mode(self, *, enabled: bool, group_id: str | None = None) -> RandomConfig:
        """Enable or disable random mode for one group."""
        with self._lock:
            if enabled:
                if group_id is None:
                    raise ChimeGroupError("group_id is required when enabling random mode")
                group = _require_group(self._groups, group_id)
                if not group.chime_filenames:
                    raise ChimeGroupError("Cannot enable random mode for an empty group")
                new_config = RandomConfig(
                    enabled=True,
                    group_id=group_id,
                    last_selected=None,
                    last_selected_at=None,
                )
            else:
                new_config = _DEFAULT_RANDOM_CONFIG
            self._persist_random_config(new_config)
            self._random_config = new_config
            return new_config

    def select_random_chime(self, group_id: str) -> str | None:
        """Select and persist a random chime for one group, avoiding repeats when possible."""
        with self._lock:
            group = _require_group(self._groups, group_id)
            if not group.chime_filenames:
                empty_config = RandomConfig(
                    enabled=self._random_config.enabled,
                    group_id=group_id,
                    last_selected=None,
                    last_selected_at=None,
                )
                self._persist_random_config(empty_config)
                self._random_config = empty_config
                return None
            available = tuple(
                filename
                for filename in group.chime_filenames
                if filename != self._random_config.last_selected
            )
            if not available:
                available = group.chime_filenames
            selected = random.SystemRandom().choice(available)
            new_config = RandomConfig(
                enabled=self._random_config.enabled,
                group_id=group_id,
                last_selected=selected,
                last_selected_at=_utc_now(),
            )
            self._persist_random_config(new_config)
            self._random_config = new_config
            return selected

    def get_active_random_chime(self) -> str | None:
        """Return the currently active random chime, if random mode is enabled."""
        with self._lock:
            if not self._random_config.enabled:
                return None
            if self._random_config.group_id is None:
                return None
            group = self._groups.get(self._random_config.group_id)
            if group is None:
                return None
            selected = self._random_config.last_selected
            if selected is None:
                return None
            if selected not in group.chime_filenames:
                return None
            return selected

    def _load_groups_or_empty(self) -> dict[str, ChimeGroup]:
        try:
            return _load_groups(self._groups_file)
        except ChimeGroupStateError as exc:
            logger.warning("Failed to load chime groups from %s: %s", self._groups_file, exc)
            return {}

    def _load_random_config_or_default(self) -> RandomConfig:
        try:
            return _load_random_config(self._random_config_file)
        except ChimeGroupStateError as exc:
            logger.warning(
                "Failed to load random chime config from %s: %s",
                self._random_config_file,
                exc,
            )
            return _DEFAULT_RANDOM_CONFIG

    def _persist_groups(self, groups: dict[str, ChimeGroup]) -> None:
        payload = {
            group_id: {
                "name": group.name,
                _GROUP_CHIMES_KEY: list(group.chime_filenames),
                "created_at": _datetime_to_json(group.created_at),
                "updated_at": _datetime_to_json(group.updated_at),
            }
            for group_id, group in groups.items()
        }
        _write_json_atomically(self._groups_file, payload)

    def _persist_random_config(self, random_config: RandomConfig) -> None:
        payload = {
            _RANDOM_CONFIG_ENABLED_KEY: random_config.enabled,
            _RANDOM_CONFIG_GROUP_ID_KEY: random_config.group_id,
            _RANDOM_CONFIG_LAST_SELECTED_KEY: random_config.last_selected,
            _RANDOM_CONFIG_LAST_SELECTED_AT_KEY: (
                _datetime_to_json(random_config.last_selected_at)
                if random_config.last_selected_at is not None
                else None
            ),
        }
        _write_json_atomically(self._random_config_file, payload)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _datetime_to_json(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_ISO_FMT)


def _datetime_from_json(raw: object, *, field_name: str) -> datetime:
    if not isinstance(raw, str):
        msg = f"{field_name} must be an ISO datetime string"
        raise ChimeGroupStateError(msg)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        msg = f"{field_name} is not a valid ISO datetime string"
        raise ChimeGroupStateError(msg) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_group_name(name: str) -> str:
    normalized = name.strip()
    if len(normalized) < _GROUP_NAME_MIN_LEN:
        msg = "Group name must not be empty"
        raise ChimeGroupError(msg)
    if len(normalized) > _GROUP_NAME_MAX_LEN:
        msg = f"Group name must be <= {_GROUP_NAME_MAX_LEN} characters"
        raise ChimeGroupError(msg)
    return normalized


def _normalize_chime_filename(chime_filename: str) -> str:
    normalized = chime_filename.strip()
    if not normalized:
        raise ChimeGroupError("Chime filename must not be empty")
    return normalized


def _normalize_chime_filenames(chime_filenames: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for chime_filename in chime_filenames:
        candidate = _normalize_chime_filename(chime_filename)
        if candidate not in normalized:
            normalized.append(candidate)
    return tuple(normalized)


def _ensure_unique_group_name(
    groups: dict[str, ChimeGroup],
    candidate_name: str,
    *,
    exclude_group_id: str | None = None,
) -> None:
    candidate_folded = candidate_name.casefold()
    for group_id, group in groups.items():
        if group_id == exclude_group_id:
            continue
        if group.name.casefold() == candidate_folded:
            msg = f"A group named '{candidate_name}' already exists"
            raise ChimeGroupError(msg)


def _require_group(groups: dict[str, ChimeGroup], group_id: str) -> ChimeGroup:
    group = groups.get(group_id)
    if group is None:
        msg = f"Group '{group_id}' was not found"
        raise ChimeGroupError(msg)
    return group


def _load_groups(path: Path) -> dict[str, ChimeGroup]:
    raw = _load_json_file(path)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ChimeGroupStateError("Groups file must contain a JSON object")
    groups: dict[str, ChimeGroup] = {}
    for raw_group_id, raw_group in raw.items():
        if not isinstance(raw_group_id, str):
            raise ChimeGroupStateError("Group IDs must be strings")
        groups[raw_group_id] = _group_from_json(raw_group_id, raw_group)
    return groups


def _group_from_json(group_id: str, raw_group: object) -> ChimeGroup:
    if not isinstance(raw_group, dict):
        msg = f"Group '{group_id}' must be a JSON object"
        raise ChimeGroupStateError(msg)
    name = raw_group.get("name")
    if not isinstance(name, str):
        msg = f"Group '{group_id}' name must be a string"
        raise ChimeGroupStateError(msg)
    filenames_raw = raw_group.get(
        _GROUP_CHIMES_KEY,
        raw_group.get(_LEGACY_CHIMES_KEY, []),
    )
    if not isinstance(filenames_raw, list) or not all(
        isinstance(item, str) for item in filenames_raw
    ):
        msg = f"Group '{group_id}' chime_filenames must be a list of strings"
        raise ChimeGroupStateError(msg)
    created_at = _datetime_from_json(raw_group.get("created_at"), field_name="created_at")
    updated_at = _datetime_from_json(raw_group.get("updated_at"), field_name="updated_at")
    return ChimeGroup(
        id=group_id,
        name=_normalize_group_name(name),
        chime_filenames=_normalize_chime_filenames(filenames_raw),
        created_at=created_at,
        updated_at=updated_at,
    )


def _load_random_config(path: Path) -> RandomConfig:
    raw = _load_json_file(path)
    if raw is None:
        return _DEFAULT_RANDOM_CONFIG
    if not isinstance(raw, dict):
        raise ChimeGroupStateError("Random config file must contain a JSON object")
    enabled = raw.get(_RANDOM_CONFIG_ENABLED_KEY, False)
    group_id = raw.get(_RANDOM_CONFIG_GROUP_ID_KEY)
    last_selected = raw.get(_RANDOM_CONFIG_LAST_SELECTED_KEY)
    last_selected_at_raw = raw.get(_RANDOM_CONFIG_LAST_SELECTED_AT_KEY)
    if not isinstance(enabled, bool):
        raise ChimeGroupStateError("Random config 'enabled' must be a boolean")
    if group_id is not None and not isinstance(group_id, str):
        raise ChimeGroupStateError("Random config 'group_id' must be a string or null")
    if last_selected is not None and not isinstance(last_selected, str):
        raise ChimeGroupStateError("Random config 'last_selected' must be a string or null")
    if last_selected_at_raw is None:
        last_selected_at = None
    else:
        last_selected_at = _datetime_from_json(
            last_selected_at_raw,
            field_name=_RANDOM_CONFIG_LAST_SELECTED_AT_KEY,
        )
    return RandomConfig(
        enabled=enabled,
        group_id=group_id,
        last_selected=last_selected,
        last_selected_at=last_selected_at,
    )


def _load_json_file(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        raw_text = path.read_text(encoding=_JSON_ENCODING)
    except OSError as exc:
        msg = f"Failed to read {path}: {exc}"
        raise ChimeGroupStateError(msg) from exc
    try:
        payload: object = json.loads(raw_text)
        return payload
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse {path}: {exc}"
        raise ChimeGroupStateError(msg) from exc


def _write_json_atomically(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}{_TMP_SUFFIX}")
    raw_json = json.dumps(payload, indent=_GROUPS_JSON_INDENT, sort_keys=True) + "\n"
    try:
        with temp_path.open("w", encoding=_JSON_ENCODING, newline="\n") as file_handle:
            file_handle.write(raw_json)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temp_path, path)  # noqa: PTH105 - spec requires os.replace for atomic publish
    except OSError as exc:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        msg = f"Failed to write {path}: {exc}"
        raise ChimeGroupStateError(msg) from exc


def make_chime_group_manager(cfg: WebConfig) -> ChimeGroupManager:
    """Build a manager using the configured B-1 state directory and filenames."""
    groups_file = cfg.paths.state_dir / cfg.chimes.groups_file_relpath
    random_config_file = cfg.paths.state_dir / cfg.chimes.random_config_relpath
    return ChimeGroupManager(groups_file=groups_file, random_config_file=random_config_file)


__all__ = (
    "ChimeGroup",
    "ChimeGroupError",
    "ChimeGroupManager",
    "ChimeGroupStateError",
    "GroupOperationResult",
    "RandomConfig",
    "make_chime_group_manager",
)
