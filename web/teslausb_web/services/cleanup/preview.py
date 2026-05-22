from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from teslausb_web.services.cleanup.discovery import ClipGroup
    from teslausb_web.services.cleanup.service import CleanupConfig
    from teslausb_web.services.storage_retention_service import RetentionPolicy

_CATEGORY_POLICY_FIELDS: dict[str, tuple[str, str]] = {
    "recent": ("keep_recent_clips", "recent_clips_days"),
    "saved": ("keep_saved_clips", "saved_clips_days"),
    "event": ("keep_event_clips", "event_clips_days"),
    "encrypted": ("keep_encrypted_clips", "encrypted_clips_days"),
}



@dataclass(frozen=True, slots=True)
class PreviewPlan:
    counts_by_category: dict[str, int]
    bytes_total: int
    sample_paths: tuple[str, ...]
    generated_at: datetime
    candidate_groups: tuple[ClipGroup, ...]
    bytes_by_category: dict[str, int]
    candidate_count: int
    protected_count: int
    current_free_bytes: int
    current_used_bytes: int
    total_capacity_bytes: int
    current_free_pct: float
    projected_free_pct: float


def build_preview_plan(
    config: CleanupConfig,
    policy: RetentionPolicy,
    groups: tuple[ClipGroup, ...],
) -> PreviewPlan:
    now = _utc_now()
    usage = _disk_usage(config)
    protected_count = 0
    age_selected: list[ClipGroup] = []
    eligible: list[ClipGroup] = []
    for group in groups:
        if _protection_reason(config, policy, group, now) is not None:
            protected_count += 1
            continue
        eligible.append(group)
        if _is_age_eligible(policy, group, now):
            age_selected.append(group)
    selected_map = {group_key(group): group for group in age_selected}
    selected_bytes = sum(group.total_bytes for group in selected_map.values())
    target_free_bytes = int(usage.total * policy.target_free_pct / 100)
    if usage.total > 0 and usage.free < target_free_bytes:
        projected_free = usage.free + selected_bytes
        for group in sorted(eligible, key=lambda item: item.oldest_modified_at):
            if projected_free >= target_free_bytes:
                break
            key = group_key(group)
            if key in selected_map:
                continue
            selected_map[key] = group
            projected_free += group.total_bytes
    candidate_groups = tuple(
        sorted(selected_map.values(), key=lambda item: (item.oldest_modified_at, item.display_path))
    )
    counts_by_category = {
        category: sum(1 for group in candidate_groups if group.category == category)
        for category in _CATEGORY_POLICY_FIELDS
    }
    bytes_by_category = {
        category: sum(group.total_bytes for group in candidate_groups if group.category == category)
        for category in _CATEGORY_POLICY_FIELDS
    }
    bytes_total = sum(group.total_bytes for group in candidate_groups)
    projected_free_bytes = usage.free + bytes_total
    current_free_pct = 0.0 if usage.total == 0 else (usage.free / usage.total) * 100.0
    projected_free_pct = 0.0 if usage.total == 0 else (projected_free_bytes / usage.total) * 100.0
    sample_paths = tuple(
        group.display_path for group in candidate_groups[: config.sample_path_limit]
    )
    return PreviewPlan(
        counts_by_category=counts_by_category,
        bytes_total=bytes_total,
        sample_paths=sample_paths,
        generated_at=now,
        candidate_groups=candidate_groups,
        bytes_by_category=bytes_by_category,
        candidate_count=len(candidate_groups),
        protected_count=protected_count,
        current_free_bytes=usage.free,
        current_used_bytes=usage.used,
        total_capacity_bytes=usage.total,
        current_free_pct=current_free_pct,
        projected_free_pct=projected_free_pct,
    )


@dataclass(frozen=True, slots=True)
class _UsageSnapshot:
    total: int
    used: int
    free: int


def _disk_usage(config: CleanupConfig) -> _UsageSnapshot:
    probe = _existing_probe_root(config)
    if probe is None:
        return _UsageSnapshot(total=0, used=0, free=0)
    usage = shutil.disk_usage(probe)
    return _UsageSnapshot(total=int(usage.total), used=int(usage.used), free=int(usage.free))


def _existing_probe_root(config: CleanupConfig) -> Path | None:
    for candidate in (config.media_root, config.media_root.parent):
        if candidate.exists():
            return candidate
    return None


def _protection_reason(
    config: CleanupConfig,
    policy: RetentionPolicy,
    group: ClipGroup,
    now: datetime,
) -> str | None:
    if group.newest_modified_at >= now - timedelta(hours=config.recent_protection_hours):
        return "recent-write-grace"
    keep_field, _ = _CATEGORY_POLICY_FIELDS[group.category]
    if bool(getattr(policy, keep_field)):
        return "policy-keep"
    if group.has_gps and not config.delete_gps_tagged_clips:
        return "gps-protected"
    return None


def _is_age_eligible(policy: RetentionPolicy, group: ClipGroup, now: datetime) -> bool:
    _, days_field = _CATEGORY_POLICY_FIELDS[group.category]
    retention_days = int(getattr(policy, days_field))
    cutoff = now - timedelta(days=retention_days)
    return group.newest_modified_at <= cutoff


def group_key(group: ClipGroup) -> str:
    return f"{group.category}:{group.recording_key}"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)
