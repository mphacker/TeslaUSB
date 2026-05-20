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

from teslausb_web.services.chime_group_service import (
    ChimeGroup,
    ChimeGroupError,
    ChimeGroupManager,
    ChimeGroupStateError,
    GroupOperationResult,
    RandomConfig,
    make_chime_group_manager,
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

__all__ = (
    "ChimeGroup",
    "ChimeGroupError",
    "ChimeGroupManager",
    "ChimeGroupStateError",
    "ChimeInfo",
    "DeleteResult",
    "FileStorageLike",
    "GroupOperationResult",
    "LockChimeAudioError",
    "LockChimeFileError",
    "RandomConfig",
    "ReencodeResult",
    "ReplaceResult",
    "UploadResult",
    "WavValidation",
    "delete_chime_file",
    "list_chime_files",
    "make_chime_group_manager",
    "normalize_audio",
    "reencode_wav_for_tesla",
    "replace_lock_chime",
    "save_pretrimmed_wav",
    "set_active_chime",
    "upload_chime_file",
    "validate_tesla_wav",
)
