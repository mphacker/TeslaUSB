"""Cleanup Service for TeslaUSB — USB-partition video cleanup.

Manages retention of videos that live INSIDE the USB drive image
(``RecentClips/``, ``SavedClips/``, ``SentryClips/``,
``EncryptedClips/`` on the part1 filesystem that the gadget exposes
to Tesla). These deletions happen via the partition mount path
(present mode: USB RO mount + ``quick_edit_part2`` is NOT used here;
edit mode: RW mount). Per-folder policies are tracked in
``cleanup_config.json``.

**Scope boundary (Phase 3a / #98):** This service NEVER touches the
SD-card ``ArchivedClips/`` folder. Retention of archived clips on the
Pi's SD card is owned by ``services.archive_watchdog`` —
``archive_watchdog.force_prune_now`` is the synchronous entry point
and it honors the ``cloud_archive.delete_unsynced`` toggle, the
``_retention_running`` duplicate-trigger guard, and the "trips are
sacred" invariant. Any new "delete archived clips" code path belongs
in ``archive_watchdog``, never here.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

from config import VIDEO_EXTENSIONS

# Configure logging
logger = logging.getLogger(__name__)

# Default cleanup policies by folder type
# These are templates - actual policies are merged with detected folders
DEFAULT_POLICY_TEMPLATES = {
    'RecentClips': {
        'enabled': False,  # Disabled by default for safety
        'age_based': {'days': 30, 'enabled': True},
        'size_based': {'max_gb': 50, 'enabled': False},
        'count_based': {'max_videos': 500, 'enabled': False}
    },
    'SavedClips': {
        'enabled': False,  # Protected by default
        'age_based': {'days': 365, 'enabled': False},
        'size_based': {'max_gb': 100, 'enabled': False},
        'count_based': {'max_videos': 1000, 'enabled': False}
    },
    'SentryClips': {
        'enabled': False,  # Protected by default
        'age_based': {'days': 90, 'enabled': False},
        'size_based': {'max_gb': 100, 'enabled': False},
        'count_based': {'max_videos': 1000, 'enabled': False}
    },
    'EncryptedClips': {
        'enabled': False,  # Protected by default (Cybertruck feature)
        'age_based': {'days': 365, 'enabled': False},
        'size_based': {'max_gb': 100, 'enabled': False},
        'count_based': {'max_videos': 1000, 'enabled': False}
    },
    # Fallback for unknown folders
    '_default': {
        'enabled': False,  # Unknown folders are protected by default
        'age_based': {'days': 90, 'enabled': False},
        'size_based': {'max_gb': 50, 'enabled': False},
        'count_based': {'max_videos': 500, 'enabled': False}
    }
}


class CleanupService:
    """Service for managing video cleanup operations"""

    def __init__(self, gadget_dir: str, config_file: str = 'cleanup_config.json'):
        """
        Initialize cleanup service

        Args:
            gadget_dir: Path to TeslaUSB installation directory
            config_file: Name of config file for cleanup policies
        """
        self.gadget_dir = Path(gadget_dir)
        self.config_path = self.gadget_dir / config_file
        self.policies = self._load_policies()

    def _get_default_policy_for_folder(self, folder_name: str) -> Dict:
        """
        Get default policy for a folder based on its name or use template

        Args:
            folder_name: Name of the folder

        Returns:
            Default policy dictionary for this folder
        """
        # Return template if it exists, otherwise use _default template
        return DEFAULT_POLICY_TEMPLATES.get(folder_name, DEFAULT_POLICY_TEMPLATES['_default']).copy()

    def _load_policies(self) -> Dict:
        """Load cleanup policies from config file or return defaults"""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r') as f:
                    policies = json.load(f)
                logger.info(f"Loaded cleanup policies from {self.config_path}")
                return policies
            except Exception as e:
                logger.error(f"Error loading cleanup policies: {e}")
                # Return empty dict - will be populated from detected folders
                return {}
        else:
            logger.info("No config file found, will use defaults for detected folders")
            return {}

    def save_policies(self, policies: Dict) -> bool:
        """
        Save cleanup policies to config file

        Args:
            policies: Dictionary of cleanup policies

        Returns:
            True if saved successfully, False otherwise
        """
        try:
            with open(self.config_path, 'w') as f:
                json.dump(policies, f, indent=2)
            self.policies = policies
            logger.info(f"Saved cleanup policies to {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving cleanup policies: {e}")
            return False

    def detect_teslacam_folders(self, partition_path: Path) -> List[str]:
        """
        Detect what folders exist in TeslaCam directory.
        Always includes standard Tesla folders so policies can be configured
        before Tesla starts recording.

        Args:
            partition_path: Path to partition mount point

        Returns:
            List of folder names (always includes RecentClips, SavedClips, SentryClips)
        """
        # Standard folders Tesla creates — always show in settings
        standard = {'RecentClips', 'SavedClips', 'SentryClips'}
        found = set()

        teslacam_path = partition_path / 'TeslaCam'
        if teslacam_path.exists():
            try:
                for item in teslacam_path.iterdir():
                    if item.is_dir() and not item.name.startswith('.'):
                        found.add(item.name)
            except Exception as e:
                logger.error(f"Error detecting TeslaCam folders: {e}")

        all_folders = standard | found
        logger.info(f"TeslaCam folders (detected: {found}, standard: {standard})")
        return sorted(all_folders)

    def get_policies_for_detected_folders(self, partition_path: Path) -> Dict:
        """
        Get policies merged with detected folders
        Existing policies are preserved, new folders get defaults

        Args:
            partition_path: Path to partition mount point

        Returns:
            Dictionary of policies for all detected folders
        """
        detected_folders = self.detect_teslacam_folders(partition_path)
        merged_policies = {}

        for folder in detected_folders:
            if folder in self.policies:
                # Use existing saved policy
                merged_policies[folder] = self.policies[folder]
            else:
                # Use default policy for this folder type
                merged_policies[folder] = self._get_default_policy_for_folder(folder)
                logger.info(f"Using default policy for new folder: {folder}")

        return merged_policies

    def get_policies(self) -> Dict:
        """Get current cleanup policies"""
        return self.policies.copy()

    def _is_video_file(self, filepath: Path) -> bool:
        """Check if file is a video based on extension"""
        return filepath.suffix.lower() in VIDEO_EXTENSIONS

    def _is_protected(self, video_info: Dict, folder: str) -> bool:
        """
        Check if video is protected from deletion

        Args:
            video_info: Dictionary with video metadata
            folder: Folder name (RecentClips, SavedClips, etc.)

        Returns:
            True if video should NOT be deleted
        """
        # 1. Videos from the past hour (might still be recording or actively used)
        one_hour_ago = datetime.now() - timedelta(hours=1)
        if video_info['date'] > one_hour_ago:
            logger.debug(f"Protected (recent - within 1 hour): {video_info['path']}")
            return True

        # 2. Videos in SavedClips/SentryClips unless explicitly enabled
        if folder in ['SavedClips', 'SentryClips']:
            if not self.policies.get(folder, {}).get('enabled', False):
                logger.debug(f"Protected ({folder} disabled): {video_info['path']}")
                return True

        # 3. Check if file is locked (being accessed)
        try:
            # Try to open file in exclusive mode
            with open(video_info['path'], 'r+b'):
                pass
        except (IOError, OSError):
            logger.debug(f"Protected (locked): {video_info['path']}")
            return True

        return False

    def _get_videos_in_folder(self, folder_path: Path, folder_name: str) -> List[Dict]:
        """
        Scan folder for video files and return metadata

        Args:
            folder_path: Path to folder
            folder_name: Name of folder (for logging)

        Returns:
            List of dictionaries with video metadata
        """
        videos = []

        if not folder_path.exists():
            logger.warning(f"Folder does not exist: {folder_path}")
            return videos

        for item in folder_path.rglob('*'):
            if item.is_file() and self._is_video_file(item):
                try:
                    stat = item.stat()
                    videos.append({
                        'path': str(item),
                        'size': stat.st_size,
                        'date': datetime.fromtimestamp(stat.st_mtime),
                        'folder': folder_name
                    })
                except Exception as e:
                    logger.error(f"Error processing {item}: {e}")

        logger.info(f"Found {len(videos)} videos in {folder_name}")
        return videos

    def calculate_cleanup_plan(self, partition_path: Path, respect_enabled_flag: bool = False) -> Dict[str, Any]:
        """
        Calculate which files should be deleted based on policies

        Args:
            partition_path: Path to TeslaCam partition mount
            respect_enabled_flag: If True, only process folders where enabled=True.
                                 If False, process all folders (for manual preview/execute).
                                 The enabled flag should only control auto-cleanup on boot.

        Returns:
            Dictionary with cleanup plan details
        """
        candidates = []
        breakdown_by_folder = {}

        for folder_name, policy in self.policies.items():
            # Only check enabled flag if respect_enabled_flag is True (for auto-cleanup on boot)
            # For manual preview/execute, we process all folders regardless of enabled flag
            if respect_enabled_flag and not policy.get('enabled', False):
                logger.info(f"Skipping {folder_name} (auto-cleanup disabled)")
                continue

            folder_path = partition_path / 'TeslaCam' / folder_name
            videos = self._get_videos_in_folder(folder_path, folder_name)

            if not videos:
                continue

            folder_candidates = []

            # Apply age-based filtering
            age_config = policy.get('age_based', {})
            if age_config.get('enabled', False):
                days = age_config.get('days', 30)
                cutoff_date = datetime.now() - timedelta(days=days)
                age_filtered = [v for v in videos if v['date'] < cutoff_date]
                logger.info(f"{folder_name}: {len(age_filtered)} videos older than {days} days")
                folder_candidates.extend(age_filtered)

            # Apply size-based filtering
            size_config = policy.get('size_based', {})
            if size_config.get('enabled', False):
                max_gb = size_config.get('max_gb', 50)
                max_bytes = max_gb * 1024**3

                # Sort by date (oldest first)
                sorted_videos = sorted(videos, key=lambda v: v['date'])
                current_size = sum(v['size'] for v in sorted_videos)

                if current_size > max_bytes:
                    # Delete oldest until under limit
                    to_delete = []
                    for video in sorted_videos:
                        if current_size <= max_bytes:
                            break
                        to_delete.append(video)
                        current_size -= video['size']

                    logger.info(f"{folder_name}: {len(to_delete)} videos exceed size limit")
                    folder_candidates.extend(to_delete)

            # Apply count-based filtering
            count_config = policy.get('count_based', {})
            if count_config.get('enabled', False):
                max_videos = count_config.get('max_videos', 500)

                if len(videos) > max_videos:
                    # Sort by date (oldest first)
                    sorted_videos = sorted(videos, key=lambda v: v['date'])
                    to_delete = sorted_videos[:-max_videos]  # Keep only max_videos newest

                    logger.info(f"{folder_name}: {len(to_delete)} videos exceed count limit")
                    folder_candidates.extend(to_delete)

            # Remove duplicates (video might match multiple criteria)
            unique_candidates = list({v['path']: v for v in folder_candidates}.values())

            # Apply protection filters
            protected_count = 0
            for video in unique_candidates[:]:
                if self._is_protected(video, folder_name):
                    unique_candidates.remove(video)
                    protected_count += 1

            logger.info(f"{folder_name}: {protected_count} videos protected from deletion")

            candidates.extend(unique_candidates)
            breakdown_by_folder[folder_name] = {
                'count': len(unique_candidates),
                'size': sum(v['size'] for v in unique_candidates),
                'videos': unique_candidates
            }

        # Calculate totals
        total_size = sum(v['size'] for v in candidates)

        # Find oldest remaining video (after deletion)
        if candidates:
            all_videos = []
            for folder_name in self.policies.keys():
                folder_path = partition_path / 'TeslaCam' / folder_name
                all_videos.extend(self._get_videos_in_folder(folder_path, folder_name))

            # Remove candidates from all_videos
            candidate_paths = {v['path'] for v in candidates}
            remaining = [v for v in all_videos if v['path'] not in candidate_paths]

            oldest_remaining = None
            if remaining:
                oldest_remaining = min(v['date'] for v in remaining).strftime('%Y-%m-%d %H:%M')
        else:
            oldest_remaining = None

        return {
            'files': candidates,
            'total_count': len(candidates),
            'total_size': total_size,
            'total_size_gb': round(total_size / 1024**3, 2),
            'breakdown_by_folder': breakdown_by_folder,
            'oldest_remaining': oldest_remaining
        }

    def preview_cleanup_impact(self, cleanup_plan: Dict, current_usage: Dict) -> Dict:
        """
        Show before/after storage projections

        Args:
            cleanup_plan: Output from calculate_cleanup_plan()
            current_usage: Current partition usage from analytics_service

        Returns:
            Dictionary with before/after comparison
        """
        freed_gb = cleanup_plan['total_size_gb']

        after_used_gb = current_usage['used_gb'] - freed_gb
        after_free_gb = current_usage['free_gb'] + freed_gb
        after_percent = (after_used_gb / current_usage['total_gb']) * 100

        return {
            'before': {
                'used_gb': current_usage['used_gb'],
                'free_gb': current_usage['free_gb'],
                'percent_used': current_usage['percent_used']
            },
            'after': {
                'used_gb': round(after_used_gb, 2),
                'free_gb': round(after_free_gb, 2),
                'percent_used': round(after_percent, 2)
            },
            'freed_gb': freed_gb
        }

    def execute_cleanup(self, cleanup_plan: Dict, dry_run: bool = False) -> Dict:
        """
        Execute the cleanup plan by deleting files

        Args:
            cleanup_plan: Output from calculate_cleanup_plan()
            dry_run: If True, don't actually delete files

        Returns:
            Dictionary with execution results
        """
        deleted_count = 0
        deleted_size = 0
        errors = []
        deleted_files = []

        for video in cleanup_plan['files']:
            try:
                if not dry_run:
                    from services.file_safety import (
                        safe_delete_archive_video, DeleteOutcome,
                    )
                    result = safe_delete_archive_video(video['path'])
                    if result.outcome is DeleteOutcome.PROTECTED:
                        # Helper already logged the BLOCKED warning;
                        # surface the user-facing reason.
                        errors.append(f"BLOCKED: {video['path']} is a protected file")
                        continue
                    if result.outcome is DeleteOutcome.MISSING:
                        errors.append(f"Skipped (missing): {video['path']}")
                        continue
                    if result.outcome is DeleteOutcome.ERROR:
                        errors.append(f"Skipped (unwritable): {video['path']}")
                        continue
                    # outcome is DELETED
                    logger.info(f"Deleted: {video['path']}")
                else:
                    logger.info(f"[DRY RUN] Would delete: {video['path']}")

                deleted_count += 1
                deleted_size += video['size']
                deleted_files.append({
                    'path': video['path'],
                    'size': video['size'],
                    'date': video['date'].strftime('%Y-%m-%d %H:%M'),
                    'folder': video['folder']
                })

            except Exception as e:
                error_msg = f"Failed to delete {video['path']}: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)

        # Purge geodata.db entries for deleted files
        if not dry_run and deleted_files:
            try:
                from config import MAPPING_ENABLED, MAPPING_DB_PATH
                if MAPPING_ENABLED:
                    from services.mapping_service import purge_deleted_videos
                    paths = [f['path'] for f in deleted_files]
                    purge_deleted_videos(MAPPING_DB_PATH, deleted_paths=paths)
            except Exception as e:
                logger.warning("Failed to purge geodata for cleaned-up videos: %s", e)

        return {
            'success': len(errors) == 0,
            'deleted_count': deleted_count,
            'deleted_size': deleted_size,
            'deleted_size_gb': round(deleted_size / 1024**3, 2),
            'deleted_files': deleted_files,
            'errors': errors,
            'dry_run': dry_run,
            'timestamp': datetime.now().isoformat()
        }

        # Purge geodata.db entries for deleted files
        if not dry_run and deleted_files:
            try:
                from config import MAPPING_ENABLED, MAPPING_DB_PATH
                if MAPPING_ENABLED:
                    from services.mapping_service import purge_deleted_videos
                    paths = [f['path'] for f in deleted_files]
                    purge_deleted_videos(MAPPING_DB_PATH, deleted_paths=paths)
            except Exception as e:
                logger.warning("Failed to purge geodata for cleaned-up videos: %s", e)

    def run_automatic_cleanup(self, partition_path: Path, dry_run: bool = False) -> Dict:
        """
        Run automatic cleanup on boot - only processes folders where enabled=True

        Args:
            partition_path: Path to TeslaCam partition mount
            dry_run: If True, don't actually delete files

        Returns:
            Dictionary with execution results
        """
        # Calculate plan with respect_enabled_flag=True for auto-cleanup
        cleanup_plan = self.calculate_cleanup_plan(partition_path, respect_enabled_flag=True)

        # Only run if there are files to delete
        if cleanup_plan['total_count'] == 0:
            logger.info("Automatic cleanup: No files to delete")
            return {
                'success': True,
                'deleted_count': 0,
                'deleted_size': 0,
                'deleted_size_gb': 0.0,
                'deleted_files': [],
                'errors': [],
                'dry_run': dry_run,
                'timestamp': datetime.now().isoformat()
            }

        # Execute cleanup
        logger.info(f"Automatic cleanup: Processing {cleanup_plan['total_count']} files")
        return self.execute_cleanup(cleanup_plan, dry_run=dry_run)

def get_cleanup_service(gadget_dir: str) -> CleanupService:
    """
    Factory function to create CleanupService instance

    Args:
        gadget_dir: Path to TeslaUSB installation directory

    Returns:
        CleanupService instance
    """
    return CleanupService(gadget_dir)


# ---------------------------------------------------------------------------
# Phase 3a.2 (#98) — One-shot migration of legacy cleanup_config.json
# ---------------------------------------------------------------------------

# Folders the storage_retention blueprint accepts as per-folder overrides.
# Anything outside this set is silently dropped so the migration can't seed
# typo'd legacy keys into config.yaml's new ``cleanup.policies`` map.
_MIGRATION_ALLOWED_FOLDERS = (
    'SentryClips',
    'SavedClips',
    'RecentClips',
    'EncryptedClips',
    'ArchivedClips',
)


def _seed_default_retention_from_legacy_yaml(cfg: Dict[str, Any]) -> Optional[int]:
    """Phase 3a.2 (#98) — preserve existing customizations across upgrades.

    On a ``git pull`` the shipped ``cleanup.default_retention_days: 0``
    means "inherit from legacy". This helper migrates the legacy value
    into the unified key so the user's customization survives even if
    they never visit the new Settings card.

    Mutates ``cfg`` in place. Returns the value seeded (or ``None`` if
    nothing was seeded — either the unified key is already set or no
    legacy value exists).
    """
    cleanup = cfg.get('cleanup') if isinstance(cfg.get('cleanup'), dict) else {}
    try:
        existing = int(cleanup.get('default_retention_days') or 0)
    except (TypeError, ValueError):
        existing = 0
    if existing > 0:
        return None  # user (or a prior boot's seed pass) already set it

    candidate: Optional[int] = None
    cloud_archive = cfg.get('cloud_archive') if isinstance(cfg.get('cloud_archive'), dict) else {}
    try:
        c = int(cloud_archive.get('archived_clips_retention_days') or 0)
        if c > 0:
            candidate = c
    except (TypeError, ValueError):
        pass
    if candidate is None:
        archive = cfg.get('archive') if isinstance(cfg.get('archive'), dict) else {}
        try:
            a = int(archive.get('retention_days') or 0)
            if a > 0:
                candidate = a
        except (TypeError, ValueError):
            pass
    if candidate is None:
        return None

    cleanup['default_retention_days'] = candidate
    cfg['cleanup'] = cleanup
    return candidate


def migrate_legacy_cleanup_config(
    gadget_dir: str,
    config_yaml_path: Optional[str] = None,
    config_filename: str = 'cleanup_config.json',
) -> Dict[str, Any]:
    """One-shot migration of the legacy ``cleanup_config.json`` AND legacy
    YAML retention keys into the unified ``cleanup`` section.

    Two passes, both idempotent:

    1. **Default-retention seed pass** — if
       ``cleanup.default_retention_days`` is unset/0, copy from
       ``cloud_archive.archived_clips_retention_days`` (or the older
       ``archive.retention_days``). Preserves user customizations
       across a ``git pull``.
    2. **Per-folder policy import pass** — if ``cleanup.policies`` is
       empty AND a legacy ``cleanup_config.json`` exists, import its
       allow-listed policies. The legacy file is renamed with a
       ``.migrated`` suffix on success so the next boot doesn't redo
       the work.

    Returns a small summary dict suitable for INFO-level logging:
    ``{'migrated': bool, 'imported_folders': [...],
       'seeded_default_retention_days': Optional[int], 'reason': str}``.

    Safe to call from service startup — never raises. A logged WARNING
    on failure is preferred over crashing the web service over a
    legacy-config corner case.
    """
    summary: Dict[str, Any] = {
        'migrated': False,
        'imported_folders': [],
        'seeded_default_retention_days': None,
        'reason': '',
    }
    try:
        # Resolve config.yaml path — fall back to the canonical helper
        # so this works under tests that monkeypatch the location.
        if config_yaml_path is None:
            try:
                from config import CONFIG_YAML  # type: ignore
                config_yaml_path = CONFIG_YAML
            except Exception:  # noqa: BLE001
                config_yaml_path = str(Path(gadget_dir) / 'config.yaml')

        try:
            import yaml  # local import keeps import-time cost off the hot path
            with open(config_yaml_path, 'r') as f:
                cfg = yaml.safe_load(f) or {}
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"migrate_legacy_cleanup_config: cannot read {config_yaml_path}: {e}")
            summary['reason'] = f'config.yaml unreadable: {e}'
            return summary

        # Pass 1: seed default_retention_days from legacy YAML keys.
        seeded = _seed_default_retention_from_legacy_yaml(cfg)
        if seeded is not None:
            summary['seeded_default_retention_days'] = seeded

        legacy_path = Path(gadget_dir) / config_filename
        legacy_exists = legacy_path.exists()

        # Pass 2: import per-folder policies from legacy JSON (if any).
        existing_policies = (
            cfg.get('cleanup', {}).get('policies') if isinstance(cfg.get('cleanup'), dict) else None
        )
        if isinstance(existing_policies, dict) and existing_policies:
            # User has already used the new UI to set policies. The
            # legacy file (e.g. left behind from a downgrade) MUST NOT
            # overwrite the user's choices.
            summary['reason'] = 'cleanup.policies already populated; skipping'
            if legacy_exists:
                try:
                    legacy_path.rename(legacy_path.with_suffix(legacy_path.suffix + '.migrated'))
                except Exception:  # noqa: BLE001
                    pass
            # Still write back if the seed pass changed something.
            if seeded is not None:
                _atomic_write_yaml(config_yaml_path, cfg, summary)
            return summary

        if not legacy_exists:
            summary['reason'] = 'no legacy file'
            # Persist any seed-pass change.
            if seeded is not None:
                _atomic_write_yaml(config_yaml_path, cfg, summary)
                if not summary['reason'].startswith('config.yaml write failed'):
                    summary['reason'] = (
                        f'no legacy file; seeded default_retention_days={seeded}'
                    )
            return summary

        try:
            with open(legacy_path, 'r') as f:
                legacy = json.load(f) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"migrate_legacy_cleanup_config: cannot parse {legacy_path}: {e}")
            summary['reason'] = f'legacy file unparseable: {e}'
            # Even if we can't parse the legacy file, persist the
            # default-retention seed so the upgrade isn't lost.
            if seeded is not None:
                _atomic_write_yaml(config_yaml_path, cfg, summary)
            return summary

        imported: Dict[str, Dict[str, Any]] = {}
        for folder, policy in legacy.items():
            if folder not in _MIGRATION_ALLOWED_FOLDERS or not isinstance(policy, dict):
                continue
            age = policy.get('age_based') if isinstance(policy.get('age_based'), dict) else {}
            try:
                days = int(age.get('days', 0)) or None
            except (TypeError, ValueError):
                days = None
            if days is None or days < 1:
                continue
            imported[folder] = {
                'enabled': bool(policy.get('enabled', False)),
                'retention_days': days,
            }

        if not imported:
            summary['reason'] = 'no migratable policies in legacy file'
            try:
                legacy_path.rename(legacy_path.with_suffix(legacy_path.suffix + '.migrated'))
            except Exception:  # noqa: BLE001
                pass
            if seeded is not None:
                _atomic_write_yaml(config_yaml_path, cfg, summary)
            return summary

        cleanup_block = cfg.get('cleanup') if isinstance(cfg.get('cleanup'), dict) else {}
        cleanup_block.setdefault('default_retention_days', 0)
        cleanup_block.setdefault('free_space_target_pct', 10)
        cleanup_block.setdefault('max_archive_size_gb', 0)
        cleanup_block.setdefault('short_retention_warning_days', 7)
        cleanup_block['policies'] = imported
        cfg['cleanup'] = cleanup_block

        if not _atomic_write_yaml(config_yaml_path, cfg, summary):
            return summary

        try:
            legacy_path.rename(legacy_path.with_suffix(legacy_path.suffix + '.migrated'))
        except Exception:  # noqa: BLE001
            # Migration succeeded; rename failure is cosmetic.
            pass

        summary['migrated'] = True
        summary['imported_folders'] = sorted(imported.keys())
        summary['reason'] = f"migrated {len(imported)} folder policies"
        logger.info(
            f"migrate_legacy_cleanup_config: imported {len(imported)} legacy policies "
            f"into cleanup.policies ({sorted(imported.keys())})"
        )
        return summary
    except Exception as e:  # noqa: BLE001
        logger.exception(f"migrate_legacy_cleanup_config: unexpected failure: {e}")
        summary['reason'] = f'unexpected: {e}'
        return summary


def _atomic_write_yaml(path: str, cfg: Dict[str, Any], summary: Dict[str, Any]) -> bool:
    """Power-loss-safe YAML write. Returns True on success; on failure
    sets ``summary['reason']`` and returns False (caller should bail).
    """
    try:
        import yaml
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w') as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"_atomic_write_yaml: failed to write {path}: {e}")
        summary['reason'] = f'config.yaml write failed: {e}'
        return False
