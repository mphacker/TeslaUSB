"""SQLite-backed tracked-license-plate service and redaction defaults."""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from teslausb_web.config import WebConfig

logger = logging.getLogger(__name__)

_DB_TIMEOUT_SECONDS: Final[float] = 10.0
_SCHEMA_VERSION: Final[int] = 1
_DEFAULT_MAX_PLATE_LENGTH: Final[int] = 16
_DEFAULT_MAX_LABEL_LENGTH: Final[int] = 64
_DEFAULT_MAX_NOTES_LENGTH: Final[int] = 240


class LicensePlateError(RuntimeError):
    """Base error raised for tracked license-plate operations."""


class PlateConfigError(ValueError):
    """Input or configuration validation failed."""


class PlateDuplicateError(LicensePlateError):
    """A normalized license plate already exists."""


class PlateNotFoundError(LicensePlateError):
    """A requested license plate ID does not exist."""


@dataclass(frozen=True, slots=True)
class LicensePlateConfig:
    db_path: Path
    default_redaction_enabled: bool = False
    max_plate_length: int = _DEFAULT_MAX_PLATE_LENGTH
    max_label_length: int = _DEFAULT_MAX_LABEL_LENGTH
    max_notes_length: int = _DEFAULT_MAX_NOTES_LENGTH

    def __post_init__(self) -> None:
        resolved_path = self.db_path.resolve()
        object.__setattr__(self, "db_path", resolved_path)
        if self.max_plate_length <= 0:
            raise PlateConfigError("max_plate_length must be > 0")
        if self.max_label_length <= 0:
            raise PlateConfigError("max_label_length must be > 0")
        if self.max_notes_length <= 0:
            raise PlateConfigError("max_notes_length must be > 0")


@dataclass(frozen=True, slots=True)
class LicensePlate:
    id: int
    plate_text: str
    normalized_plate: str
    label: str
    notes: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PlateMatch:
    candidate: str
    normalized_candidate: str
    matched_plate: LicensePlate | None

    @property
    def is_match(self) -> bool:
        return self.matched_plate is not None


@dataclass(frozen=True, slots=True)
class RedactionConfig:
    enabled: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PlateBulkOperationResult:
    requested_count: int
    deleted_count: int
    missing_ids: tuple[int, ...]
    message: str

    @property
    def success(self) -> bool:
        return self.deleted_count > 0 and not self.missing_ids


class LicensePlateService:
    """Store tracked license plates and the default redaction toggle."""

    def __init__(self, config: LicensePlateConfig) -> None:
        self._config = config
        self._db_path = config.db_path
        self._lock = threading.RLock()

    @property
    def config(self) -> LicensePlateConfig:
        return self._config

    @contextmanager
    def open_db(self) -> Iterator[sqlite3.Connection]:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(str(self._db_path), timeout=_DB_TIMEOUT_SECONDS)
        except sqlite3.Error as exc:
            raise LicensePlateError(f"Failed to open license plate database: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            self._ensure_schema(connection)
            yield connection
        except sqlite3.Error as exc:
            connection.rollback()
            raise LicensePlateError(f"License plate database error: {exc}") from exc
        finally:
            connection.close()

    def list_license_plates(self) -> tuple[LicensePlate, ...]:
        with self._lock, self.open_db() as connection:
            rows = self._fetch_rows(
                connection,
                (
                    "SELECT id, plate_text, normalized_plate, label, notes, created_at, updated_at "
                    "FROM tracked_license_plates ORDER BY normalized_plate ASC, id ASC"
                ),
            )
        return tuple(_plate_from_row(row) for row in rows)

    def count_license_plates(self) -> int:
        with self._lock, self.open_db() as connection:
            row = self._fetch_required_row(
                connection,
                "SELECT COUNT(*) AS count FROM tracked_license_plates",
            )
        return _require_int(row, "count")

    def get_redaction_config(self) -> RedactionConfig:
        with self._lock, self.open_db() as connection:
            row = self._fetch_required_row(
                connection,
                "SELECT enabled, updated_at FROM plate_redaction_config WHERE config_id = 1",
            )
        return _redaction_from_row(row)

    def add_license_plate(
        self,
        plate_text: str,
        *,
        label: str = "",
        notes: str = "",
    ) -> LicensePlate:
        normalized_plate = _normalize_plate(plate_text, max_length=self._config.max_plate_length)
        clean_label = _normalize_free_text(
            label,
            field_name="label",
            max_length=self._config.max_label_length,
        )
        clean_notes = _normalize_free_text(
            notes,
            field_name="notes",
            max_length=self._config.max_notes_length,
        )
        timestamp = _utc_now().isoformat()
        with self._lock, self.open_db() as connection:
            try:
                cursor = connection.execute(
                    (
                        "INSERT INTO tracked_license_plates "
                        "(plate_text, normalized_plate, label, notes, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        normalized_plate,
                        normalized_plate,
                        clean_label,
                        clean_notes,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PlateDuplicateError(
                    f"Tracked plate already exists: {normalized_plate}"
                ) from exc
            connection.commit()
            plate_id = cursor.lastrowid
            if not isinstance(plate_id, int):
                raise LicensePlateError("Database did not return a license plate ID")
            row = self._fetch_required_row(
                connection,
                (
                    "SELECT id, plate_text, normalized_plate, label, notes, created_at, updated_at "
                    "FROM tracked_license_plates WHERE id = ?"
                ),
                (plate_id,),
            )
        logger.info("Added tracked license plate %s", normalized_plate)
        return _plate_from_row(row)

    def update_license_plate(
        self,
        plate_id: int,
        *,
        plate_text: str,
        label: str = "",
        notes: str = "",
    ) -> LicensePlate:
        resolved_id = _require_positive_id(plate_id, field_name="plate_id")
        normalized_plate = _normalize_plate(plate_text, max_length=self._config.max_plate_length)
        clean_label = _normalize_free_text(
            label,
            field_name="label",
            max_length=self._config.max_label_length,
        )
        clean_notes = _normalize_free_text(
            notes,
            field_name="notes",
            max_length=self._config.max_notes_length,
        )
        timestamp = _utc_now().isoformat()
        with self._lock, self.open_db() as connection:
            try:
                row_count = connection.execute(
                    (
                        "UPDATE tracked_license_plates SET plate_text = ?, normalized_plate = ?, "
                        "label = ?, notes = ?, updated_at = ? WHERE id = ?"
                    ),
                    (
                        normalized_plate,
                        normalized_plate,
                        clean_label,
                        clean_notes,
                        timestamp,
                        resolved_id,
                    ),
                ).rowcount
            except sqlite3.IntegrityError as exc:
                raise PlateDuplicateError(
                    f"Tracked plate already exists: {normalized_plate}"
                ) from exc
            if row_count == 0:
                raise PlateNotFoundError(f"Tracked plate not found: {resolved_id}")
            connection.commit()
            row = self._fetch_required_row(
                connection,
                (
                    "SELECT id, plate_text, normalized_plate, label, notes, created_at, updated_at "
                    "FROM tracked_license_plates WHERE id = ?"
                ),
                (resolved_id,),
            )
        logger.info("Updated tracked license plate %s", normalized_plate)
        return _plate_from_row(row)

    def delete_license_plate(self, plate_id: int) -> bool:
        resolved_id = _require_positive_id(plate_id, field_name="plate_id")
        with self._lock, self.open_db() as connection:
            row_count = connection.execute(
                "DELETE FROM tracked_license_plates WHERE id = ?",
                (resolved_id,),
            ).rowcount
            if row_count == 0:
                raise PlateNotFoundError(f"Tracked plate not found: {resolved_id}")
            connection.commit()
        logger.info("Deleted tracked license plate id=%s", resolved_id)
        return True

    def bulk_delete(self, plate_ids: Sequence[int]) -> PlateBulkOperationResult:
        unique_ids = tuple(
            dict.fromkeys(
                _require_positive_id(plate_id, field_name="plate_id") for plate_id in plate_ids
            )
        )
        if not unique_ids:
            raise PlateConfigError("At least one plate ID is required")
        deleted_count = 0
        missing_ids_list: list[int] = []
        with self._lock, self.open_db() as connection:
            for plate_id in unique_ids:
                row = self._fetch_optional_row(
                    connection,
                    "SELECT id FROM tracked_license_plates WHERE id = ?",
                    (plate_id,),
                )
                if row is None:
                    missing_ids_list.append(plate_id)
                    continue
                connection.execute(
                    "DELETE FROM tracked_license_plates WHERE id = ?",
                    (plate_id,),
                )
                deleted_count += 1
            if deleted_count > 0:
                connection.commit()
            missing_ids = tuple(missing_ids_list)
        if deleted_count > 0:
            logger.info("Bulk deleted %s tracked license plate(s)", deleted_count)
        return PlateBulkOperationResult(
            requested_count=len(unique_ids),
            deleted_count=deleted_count,
            missing_ids=missing_ids,
            message=_bulk_delete_message(deleted_count=deleted_count, missing_ids=missing_ids),
        )

    def update_redaction_config(self, *, enabled: bool) -> RedactionConfig:
        timestamp = _utc_now().isoformat()
        with self._lock, self.open_db() as connection:
            connection.execute(
                (
                    "UPDATE plate_redaction_config SET enabled = ?, updated_at = ? "
                    "WHERE config_id = 1"
                ),
                (1 if enabled else 0, timestamp),
            )
            connection.commit()
            row = self._fetch_required_row(
                connection,
                "SELECT enabled, updated_at FROM plate_redaction_config WHERE config_id = 1",
            )
        logger.info("Updated license plate redaction default to enabled=%s", enabled)
        return _redaction_from_row(row)

    def match_plate(self, candidate: str) -> PlateMatch:
        normalized_candidate = _normalize_plate(
            candidate,
            max_length=self._config.max_plate_length,
        )
        with self._lock, self.open_db() as connection:
            row = self._fetch_optional_row(
                connection,
                (
                    "SELECT id, plate_text, normalized_plate, label, notes, created_at, updated_at "
                    "FROM tracked_license_plates WHERE normalized_plate = ?"
                ),
                (normalized_candidate,),
            )
        return PlateMatch(
            candidate=candidate,
            normalized_candidate=normalized_candidate,
            matched_plate=None if row is None else _plate_from_row(row),
        )

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS license_plate_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS tracked_license_plates ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "plate_text TEXT NOT NULL, "
            "normalized_plate TEXT NOT NULL UNIQUE, "
            "label TEXT NOT NULL DEFAULT '', "
            "notes TEXT NOT NULL DEFAULT '', "
            "created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS plate_redaction_config ("
            "config_id INTEGER PRIMARY KEY CHECK (config_id = 1), "
            "enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)), "
            "updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO license_plate_meta (key, value) VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        row = self._fetch_optional_row(
            connection,
            "SELECT enabled, updated_at FROM plate_redaction_config WHERE config_id = 1",
        )
        if row is None:
            timestamp = _utc_now().isoformat()
            connection.execute(
                (
                    "INSERT INTO plate_redaction_config (config_id, enabled, updated_at) "
                    "VALUES (1, ?, ?)"
                ),
                (1 if self._config.default_redaction_enabled else 0, timestamp),
            )
        connection.commit()

    def _fetch_rows(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: tuple[object, ...] = (),
    ) -> tuple[sqlite3.Row, ...]:
        rows = tuple(connection.execute(sql, params).fetchall())
        for row in rows:
            if not isinstance(row, sqlite3.Row):
                raise LicensePlateError("Expected sqlite3.Row result")
        return rows

    def _fetch_optional_row(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: tuple[object, ...] = (),
    ) -> sqlite3.Row | None:
        row = connection.execute(sql, params).fetchone()
        if row is None:
            return None
        if not isinstance(row, sqlite3.Row):
            raise LicensePlateError("Expected sqlite3.Row result")
        return row

    def _fetch_required_row(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: tuple[object, ...] = (),
    ) -> sqlite3.Row:
        row = self._fetch_optional_row(connection, sql, params)
        if row is None:
            raise LicensePlateError("Expected database row but got none")
        return row


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_plate(raw_plate: str, *, max_length: int) -> str:
    compact = "".join(character for character in raw_plate.upper() if character.isalnum())
    if not compact:
        raise PlateConfigError("License plate is required")
    if len(compact) > max_length:
        raise PlateConfigError(
            f"License plate must be {max_length} characters or fewer after normalization"
        )
    return compact


def _normalize_free_text(raw_text: str, *, field_name: str, max_length: int) -> str:
    compact = " ".join(raw_text.strip().split())
    if len(compact) > max_length:
        raise PlateConfigError(f"{field_name} must be {max_length} characters or fewer")
    return compact


def _require_positive_id(value: int, *, field_name: str) -> int:
    if value <= 0:
        raise PlateConfigError(f"{field_name} must be a positive integer")
    return value


def _plate_from_row(row: sqlite3.Row) -> LicensePlate:
    return LicensePlate(
        id=_require_int(row, "id"),
        plate_text=_require_str(row, "plate_text"),
        normalized_plate=_require_str(row, "normalized_plate"),
        label=_require_str(row, "label"),
        notes=_require_str(row, "notes"),
        created_at=_parse_timestamp(_require_str(row, "created_at"), field_name="created_at"),
        updated_at=_parse_timestamp(_require_str(row, "updated_at"), field_name="updated_at"),
    )


def _redaction_from_row(row: sqlite3.Row) -> RedactionConfig:
    return RedactionConfig(
        enabled=_require_boolish(row, "enabled"),
        updated_at=_parse_timestamp(_require_str(row, "updated_at"), field_name="updated_at"),
    )


def _parse_timestamp(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LicensePlateError(f"Invalid {field_name} timestamp: {value}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _require_str(row: sqlite3.Row, key: str) -> str:
    value = row[key]
    if isinstance(value, str):
        return value
    raise LicensePlateError(f"Expected {key} to be str, got {type(value).__name__}")


def _require_int(row: sqlite3.Row, key: str) -> int:
    value = row[key]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise LicensePlateError(f"Expected {key} to be int, got {type(value).__name__}")


def _require_boolish(row: sqlite3.Row, key: str) -> bool:
    value = row[key]
    if value in (0, 1):
        return bool(value)
    raise LicensePlateError(f"Expected {key} to be 0 or 1, got {type(value).__name__}")


def _bulk_delete_message(*, deleted_count: int, missing_ids: tuple[int, ...]) -> str:
    if deleted_count == 0 and missing_ids:
        return "No tracked plates were deleted"
    if missing_ids:
        return f"Deleted {deleted_count} tracked plate(s); missing IDs: " + ", ".join(
            str(plate_id) for plate_id in missing_ids
        )
    return f"Deleted {deleted_count} tracked plate(s)"


def make_license_plate_service(
    cfg: WebConfig | LicensePlateConfig,
) -> LicensePlateService:
    if isinstance(cfg, LicensePlateConfig):
        return LicensePlateService(cfg)
    return LicensePlateService(
        LicensePlateConfig(
            db_path=cfg.license_plates.db_path,
            default_redaction_enabled=cfg.license_plates.default_redaction_enabled,
            max_plate_length=cfg.license_plates.max_plate_length,
            max_label_length=cfg.license_plates.max_label_length,
            max_notes_length=cfg.license_plates.max_notes_length,
        )
    )


__all__ = (
    "LicensePlate",
    "LicensePlateConfig",
    "LicensePlateError",
    "LicensePlateService",
    "PlateBulkOperationResult",
    "PlateConfigError",
    "PlateDuplicateError",
    "PlateMatch",
    "PlateNotFoundError",
    "RedactionConfig",
    "make_license_plate_service",
)
