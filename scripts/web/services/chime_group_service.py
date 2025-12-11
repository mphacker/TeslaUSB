"""
Chime Group Service - Manage lock chime groups for random selection.

This module provides functionality for:
- Creating and managing chime groups (e.g., "Holidays", "Funny", "Seasonal")
- Assigning chimes to groups
- Selecting random chimes from a specific group
- Persisting group configuration to JSON
"""

import os
import json
import random
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config import GADGET_DIR

logger = logging.getLogger(__name__)

# Group storage file
GROUPS_FILE = os.path.join(GADGET_DIR, 'chime_groups.json')

# Random mode configuration file
RANDOM_CONFIG_FILE = os.path.join(GADGET_DIR, 'chime_random_config.json')


class ChimeGroupManager:
    """Manages chime groups and random selection configuration."""
    
    def __init__(self, groups_file=None, random_config_file=None):
        """Initialize manager with file paths."""
        self.groups_file = groups_file or GROUPS_FILE
        self.random_config_file = random_config_file or RANDOM_CONFIG_FILE
        
        try:
            self.groups = self._load_groups()
        except Exception as e:
            logger.error(f"Failed to load groups during init: {e}")
            self.groups = {}
        
        try:
            self.random_config = self._load_random_config()
        except Exception as e:
            logger.error(f"Failed to load random config during init: {e}")
            self.random_config = {
                'enabled': False,
                'group_id': None,
                'last_selected': None,
                'updated_at': None
            }
    
    def _load_groups(self) -> Dict[str, Dict]:
        """Load groups from JSON file."""
        if not os.path.exists(self.groups_file):
            return {}
        
        try:
            with open(self.groups_file, 'r') as f:
                groups = json.load(f)
                logger.info(f"Loaded {len(groups)} chime groups")
                return groups
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Error loading groups: {e}")
            return {}
    
    def _save_groups(self) -> bool:
        """Save groups to JSON file."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.groups_file), exist_ok=True)
            
            with open(self.groups_file, 'w') as f:
                json.dump(self.groups, f, indent=2)
            
            logger.info(f"Saved {len(self.groups)} chime groups")
            return True
        except OSError as e:
            logger.error(f"Error saving groups: {e}")
            return False
    
    def _load_random_config(self) -> Dict:
        """Load random mode configuration."""
        if not os.path.exists(self.random_config_file):
            return {
                'enabled': False,
                'group_id': None,
                'last_selected': None,
                'updated_at': None
            }
        
        try:
            with open(self.random_config_file, 'r') as f:
                config = json.load(f)
                logger.info(f"Loaded random config: enabled={config.get('enabled')}, group={config.get('group_id')}")
                return config
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Error loading random config: {e}")
            return {
                'enabled': False,
                'group_id': None,
                'last_selected': None,
                'updated_at': None
            }
    
    def _save_random_config(self) -> bool:
        """Save random mode configuration."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.random_config_file), exist_ok=True)
            
            with open(self.random_config_file, 'w') as f:
                json.dump(self.random_config, f, indent=2)
            
            logger.info(f"Saved random config: enabled={self.random_config.get('enabled')}, group={self.random_config.get('group_id')}")
            return True
        except OSError as e:
            logger.error(f"Error saving random config: {e}")
            return False
    
    def list_groups(self) -> List[Dict]:
        """
        Get all groups with metadata.
        
        Returns:
            List of group dictionaries with id, name, description, chimes, etc.
        """
        groups_list = []
        for group_id, group_data in self.groups.items():
            groups_list.append({
                'id': group_id,
                'name': group_data.get('name', group_id),
                'description': group_data.get('description', ''),
                'chimes': group_data.get('chimes', []),
                'chime_count': len(group_data.get('chimes', [])),
                'created_at': group_data.get('created_at'),
                'updated_at': group_data.get('updated_at')
            })
        
        # Sort by name
        groups_list.sort(key=lambda g: g['name'].lower())
        return groups_list
    
    def get_group(self, group_id: str) -> Optional[Dict]:
        """
        Get a specific group by ID.
        
        Args:
            group_id: Group identifier
        
        Returns:
            Group dictionary or None if not found
        """
        if group_id not in self.groups:
            return None
        
        group_data = self.groups[group_id]
        return {
            'id': group_id,
            'name': group_data.get('name', group_id),
            'description': group_data.get('description', ''),
            'chimes': group_data.get('chimes', []),
            'chime_count': len(group_data.get('chimes', [])),
            'created_at': group_data.get('created_at'),
            'updated_at': group_data.get('updated_at')
        }
    
    def create_group(self, name: str, description: str = '', chimes: List[str] = None) -> Tuple[bool, str, Optional[str]]:
        """
        Create a new chime group.
        
        Args:
            name: Group name
            description: Optional description
            chimes: List of chime filenames
        
        Returns:
            (success, message, group_id)
        """
        # Validate name
        if not name or not name.strip():
            return False, "Group name is required", None
        
        name = name.strip()
        
        # Check for duplicate names
        for group_id, group_data in self.groups.items():
            if group_data.get('name', '').lower() == name.lower():
                return False, f"A group with name '{name}' already exists", None
        
        # Generate unique ID from name
        group_id = name.lower().replace(' ', '_').replace('-', '_')
        group_id = ''.join(c for c in group_id if c.isalnum() or c == '_')
        
        # Ensure uniqueness
        if group_id in self.groups:
            counter = 2
            while f"{group_id}_{counter}" in self.groups:
                counter += 1
            group_id = f"{group_id}_{counter}"
        
        # Create group
        self.groups[group_id] = {
            'name': name,
            'description': description,
            'chimes': chimes or [],
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        }
        
        if self._save_groups():
            logger.info(f"Created group '{name}' (id: {group_id})")
            return True, f"Group '{name}' created successfully", group_id
        else:
            # Remove from memory if save failed
            del self.groups[group_id]
            return False, "Failed to save group", None
    
    def update_group(self, group_id: str, name: str = None, description: str = None, 
                    chimes: List[str] = None) -> Tuple[bool, str]:
        """
        Update an existing group.
        
        Args:
            group_id: Group identifier
            name: New name (optional)
            description: New description (optional)
            chimes: New chimes list (optional)
        
        Returns:
            (success, message)
        """
        if group_id not in self.groups:
            return False, f"Group '{group_id}' not found"
        
        # Check for duplicate names if renaming
        if name is not None:
            name = name.strip()
            if not name:
                return False, "Group name cannot be empty"
            
            for gid, gdata in self.groups.items():
                if gid != group_id and gdata.get('name', '').lower() == name.lower():
                    return False, f"A group with name '{name}' already exists"
        
        # Update fields
        if name is not None:
            self.groups[group_id]['name'] = name
        if description is not None:
            self.groups[group_id]['description'] = description
        if chimes is not None:
            self.groups[group_id]['chimes'] = chimes
        
        self.groups[group_id]['updated_at'] = datetime.now().isoformat()
        
        if self._save_groups():
            logger.info(f"Updated group '{group_id}'")
            return True, "Group updated successfully"
        else:
            return False, "Failed to save group changes"
    
    def delete_group(self, group_id: str) -> Tuple[bool, str]:
        """
        Delete a group.
        
        Args:
            group_id: Group identifier
        
        Returns:
            (success, message)
        """
        if group_id not in self.groups:
            return False, f"Group '{group_id}' not found"
        
        # Check if this group is currently used for random mode
        if self.random_config.get('enabled') and self.random_config.get('group_id') == group_id:
            return False, "Cannot delete group: currently used for random chime selection. Disable random mode first."
        
        group_name = self.groups[group_id].get('name', group_id)
        del self.groups[group_id]
        
        if self._save_groups():
            logger.info(f"Deleted group '{group_id}'")
            return True, f"Group '{group_name}' deleted successfully"
        else:
            return False, "Failed to save changes"
    
    def add_chime_to_group(self, group_id: str, chime_filename: str) -> Tuple[bool, str]:
        """
        Add a chime to a group.
        
        Args:
            group_id: Group identifier
            chime_filename: Chime filename to add
        
        Returns:
            (success, message)
        """
        if group_id not in self.groups:
            return False, f"Group '{group_id}' not found"
        
        chimes = self.groups[group_id].get('chimes', [])
        
        if chime_filename in chimes:
            return False, f"Chime '{chime_filename}' is already in this group"
        
        chimes.append(chime_filename)
        self.groups[group_id]['chimes'] = chimes
        self.groups[group_id]['updated_at'] = datetime.now().isoformat()
        
        if self._save_groups():
            logger.info(f"Added chime '{chime_filename}' to group '{group_id}'")
            return True, f"Added to group successfully"
        else:
            return False, "Failed to save changes"
    
    def remove_chime_from_group(self, group_id: str, chime_filename: str) -> Tuple[bool, str]:
        """
        Remove a chime from a group.
        
        Args:
            group_id: Group identifier
            chime_filename: Chime filename to remove
        
        Returns:
            (success, message)
        """
        if group_id not in self.groups:
            return False, f"Group '{group_id}' not found"
        
        chimes = self.groups[group_id].get('chimes', [])
        
        if chime_filename not in chimes:
            return False, f"Chime '{chime_filename}' is not in this group"
        
        chimes.remove(chime_filename)
        self.groups[group_id]['chimes'] = chimes
        self.groups[group_id]['updated_at'] = datetime.now().isoformat()
        
        if self._save_groups():
            logger.info(f"Removed chime '{chime_filename}' from group '{group_id}'")
            return True, f"Removed from group successfully"
        else:
            return False, "Failed to save changes"
    
    def get_random_config(self) -> Dict:
        """Get current random mode configuration."""
        return self.random_config.copy()
    
    def set_random_mode(self, enabled: bool, group_id: Optional[str] = None) -> Tuple[bool, str]:
        """
        Enable or disable random chime mode.
        
        Args:
            enabled: True to enable random mode, False to disable
            group_id: Group to select from (required if enabled=True)
        
        Returns:
            (success, message)
        """
        if enabled:
            # Validate group exists and has chimes
            if not group_id:
                return False, "Group ID is required when enabling random mode"
            
            if group_id not in self.groups:
                return False, f"Group '{group_id}' not found"
            
            chimes = self.groups[group_id].get('chimes', [])
            if not chimes:
                return False, f"Group has no chimes. Add chimes to the group before enabling random mode."
            
            self.random_config['enabled'] = True
            self.random_config['group_id'] = group_id
            self.random_config['updated_at'] = datetime.now().isoformat()
            
            logger.info(f"Enabled random mode with group '{group_id}'")
        else:
            self.random_config['enabled'] = False
            self.random_config['group_id'] = None
            self.random_config['last_selected'] = None
            self.random_config['updated_at'] = datetime.now().isoformat()
            
            logger.info("Disabled random mode")
        
        if self._save_random_config():
            return True, "Random mode configuration updated"
        else:
            return False, "Failed to save random mode configuration"
    
    def select_random_chime(self, avoid_chime: Optional[str] = None, 
                           use_seed: bool = True) -> Optional[str]:
        """
        Select a random chime from the configured group.
        
        Args:
            avoid_chime: Chime filename to avoid (e.g., currently active chime)
            use_seed: If True, use system time as seed for better randomness
        
        Returns:
            Selected chime filename or None if no valid chimes
        """
        if not self.random_config.get('enabled'):
            logger.warning("Random mode is not enabled")
            return None
        
        group_id = self.random_config.get('group_id')
        if not group_id or group_id not in self.groups:
            logger.error(f"Random mode enabled but group '{group_id}' not found")
            return None
        
        chimes = self.groups[group_id].get('chimes', [])
        if not chimes:
            logger.error(f"Group '{group_id}' has no chimes")
            return None
        
        # Use high-resolution time as seed for better randomness
        if use_seed:
            import time
            random.seed(int(time.time() * 1000000))  # Microsecond precision
        
        # Filter out the chime to avoid
        available_chimes = [c for c in chimes if c != avoid_chime]
        
        # If all chimes filtered out (only 1 chime in group and it's active), use all
        if not available_chimes:
            available_chimes = chimes
        
        selected = random.choice(available_chimes)
        
        # Record selection
        self.random_config['last_selected'] = selected
        self.random_config['last_selected_at'] = datetime.now().isoformat()
        self._save_random_config()
        
        logger.info(f"Randomly selected chime: {selected} from group '{group_id}' ({len(available_chimes)} options)")
        return selected


# Singleton instance
_manager = None


def get_group_manager() -> ChimeGroupManager:
    """Get singleton ChimeGroupManager instance."""
    global _manager
    if _manager is None:
        _manager = ChimeGroupManager()
    return _manager
