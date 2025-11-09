"""
Cleanup Service for TeslaUSB
Handles automatic cleanup of old video recordings with safety mechanisms.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

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

# Video file extensions
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mov', '.mkv'}


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
        Detect what folders actually exist in TeslaCam directory
        
        Args:
            partition_path: Path to partition mount point
            
        Returns:
            List of folder names that exist
        """
        teslacam_path = partition_path / 'TeslaCam'
        
        if not teslacam_path.exists():
            logger.warning(f"TeslaCam directory not found: {teslacam_path}")
            return []
        
        folders = []
        try:
            for item in teslacam_path.iterdir():
                if item.is_dir() and not item.name.startswith('.'):
                    folders.append(item.name)
            logger.info(f"Detected TeslaCam folders: {folders}")
        except Exception as e:
            logger.error(f"Error detecting TeslaCam folders: {e}")
        
        return sorted(folders)
    
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
                    os.remove(video['path'])
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
    
    def cleanup_orphaned_thumbnails(self, thumbnail_dir: Path, video_paths: set) -> Dict:
        """
        Remove thumbnails for videos that no longer exist
        
        Args:
            thumbnail_dir: Path to thumbnail directory
            video_paths: Set of existing video file paths
            
        Returns:
            Dictionary with cleanup results
        """
        if not thumbnail_dir.exists():
            return {'removed': 0, 'errors': []}
        
        removed = 0
        errors = []
        
        # This would need to be integrated with thumbnail_generator.py's naming scheme
        # For now, just a placeholder
        logger.info("Orphaned thumbnail cleanup not yet implemented")
        
        return {
            'removed': removed,
            'errors': errors
        }


def get_cleanup_service(gadget_dir: str) -> CleanupService:
    """
    Factory function to create CleanupService instance
    
    Args:
        gadget_dir: Path to TeslaUSB installation directory
        
    Returns:
        CleanupService instance
    """
    return CleanupService(gadget_dir)
