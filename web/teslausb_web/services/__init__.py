"""Business-orchestration services.

Layer 2 in the hexagonal layering: blueprints call services;
services orchestrate domain objects and infrastructure adapters
(`config`, `db`, `ipc`). **No `flask` imports here** — services
must be callable from a non-Flask context (tests, future CLI tools,
the worker process) per `docs/03-CODE-QUALITY-CHARTER.md`
§"Architectural Principles / The Layering Rule".

Charter §"Test discipline" sets the bar:
    Coverage gate: ≥ 80% line coverage on
    `web/teslausb_web/services/`

Phase 0.3 establishes the empty subpackage; service modules arrive
incrementally in Phase 5 (one per feature area, with charter-review
per file).
"""

from __future__ import annotations

from teslausb_web.services.boombox_service import (
    BoomboxConfig,
    BoomboxError,
    BoomboxFile,
    BoomboxFileError,
    BoomboxListing,
    BoomboxService,
    make_boombox_service,
)
from teslausb_web.services.boombox_service import (
    DeleteResult as BoomboxDeleteResult,
)
from teslausb_web.services.boombox_service import (
    UploadResult as BoomboxUploadResult,
)
from teslausb_web.services.chime_group_service import (
    ChimeGroup,
    ChimeGroupError,
    ChimeGroupManager,
    ChimeGroupStateError,
    GroupOperationResult,
    RandomConfig,
    make_chime_group_manager,
)
from teslausb_web.services.chime_scheduler import (
    ActiveChimeResolution,
    ChimeScheduleError,
    ChimeScheduler,
    ChimeScheduleStateError,
    DateSchedule,
    HolidaySchedule,
    RecurringSchedule,
    Schedule,
    ScheduleOperationResult,
    WeeklySchedule,
    format_last_run,
    format_schedule_display,
    make_chime_scheduler,
)
from teslausb_web.services.lock_chime_service import (
    ChimeInfo,
    DeleteResult,
    FileStorageLike,
    LockChimeAudioError,
    LockChimeFileError,
    ReencodeResult,
    ReplaceResult,
    UploadResult,
    WavValidation,
    delete_chime_file,
    list_chime_files,
    normalize_audio,
    reencode_wav_for_tesla,
    replace_lock_chime,
    save_pretrimmed_wav,
    set_active_chime,
    upload_chime_file,
    validate_tesla_wav,
)
from teslausb_web.services.mapping_migrations import (
    _BACKUP_RETENTION,
    _SCHEMA_VERSION,
    MappingDatabaseError,
    MappingMigrationError,
    MigrationsConfig,
    MigrationsRunner,
    _backup_db,
    _init_db,
    make_migrations_runner,
)
from teslausb_web.services.wrap_service import (
    ValidationResult,
    WrapError,
    WrapFileError,
    WrapInfo,
    WrapService,
    make_wrap_service,
)

__all__ = (
    "_BACKUP_RETENTION",
    "_SCHEMA_VERSION",
    "ActiveChimeResolution",
    "BoomboxConfig",
    "BoomboxDeleteResult",
    "BoomboxError",
    "BoomboxFile",
    "BoomboxFileError",
    "BoomboxListing",
    "BoomboxService",
    "BoomboxUploadResult",
    "ChimeGroup",
    "ChimeGroupError",
    "ChimeGroupManager",
    "ChimeGroupStateError",
    "ChimeInfo",
    "ChimeScheduleError",
    "ChimeScheduleStateError",
    "ChimeScheduler",
    "DateSchedule",
    "DeleteResult",
    "FileStorageLike",
    "GroupOperationResult",
    "HolidaySchedule",
    "LockChimeAudioError",
    "LockChimeFileError",
    "MappingDatabaseError",
    "MappingMigrationError",
    "MigrationsConfig",
    "MigrationsRunner",
    "RandomConfig",
    "RecurringSchedule",
    "ReencodeResult",
    "ReplaceResult",
    "Schedule",
    "ScheduleOperationResult",
    "UploadResult",
    "ValidationResult",
    "WavValidation",
    "WeeklySchedule",
    "WrapError",
    "WrapFileError",
    "WrapInfo",
    "WrapService",
    "_backup_db",
    "_init_db",
    "delete_chime_file",
    "format_last_run",
    "format_schedule_display",
    "list_chime_files",
    "make_boombox_service",
    "make_chime_group_manager",
    "make_chime_scheduler",
    "make_migrations_runner",
    "make_wrap_service",
    "normalize_audio",
    "reencode_wav_for_tesla",
    "replace_lock_chime",
    "save_pretrimmed_wav",
    "set_active_chime",
    "upload_chime_file",
    "validate_tesla_wav",
)
