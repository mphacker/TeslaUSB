"""
Chime scheduler service for managing scheduled lock chime changes.

Stores schedules in JSON format with user-friendly time/day selections.
Calculates which chime should be active at any given time.
"""

import os
import json
import logging
from datetime import datetime, time as datetime_time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import GADGET_DIR

logger = logging.getLogger(__name__)

# Schedule storage file
SCHEDULE_FILE = os.path.join(GADGET_DIR, 'chime_schedules.json')

# Days of week (0=Monday, 6=Sunday for Python datetime)
DAYS_OF_WEEK = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


class ChimeScheduler:
    """Manages chime schedules and determines active chime."""
    
    def __init__(self, schedule_file=None):
        """Initialize scheduler with schedule file path."""
        self.schedule_file = schedule_file or SCHEDULE_FILE
        self.schedules = self._load_schedules()
    
    def _load_schedules(self) -> List[Dict]:
        """Load schedules from JSON file."""
        if not os.path.exists(self.schedule_file):
            return []
        
        try:
            with open(self.schedule_file, 'r') as f:
                schedules = json.load(f)
                logger.info(f"Loaded {len(schedules)} schedules")
                return schedules
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Error loading schedules: {e}")
            return []
    
    def _save_schedules(self) -> bool:
        """Save schedules to JSON file."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.schedule_file), exist_ok=True)
            
            with open(self.schedule_file, 'w') as f:
                json.dump(self.schedules, f, indent=2)
            
            logger.info(f"Saved {len(self.schedules)} schedules")
            return True
        except OSError as e:
            logger.error(f"Error saving schedules: {e}")
            return False
    
    def validate_schedule_conflict(self, time_str: str, days: List[str], 
                                   exclude_schedule_id: Optional[int] = None) -> Tuple[bool, Optional[str]]:
        """
        Check if a schedule conflicts with existing schedules.
        
        Args:
            time_str: Time in HH:MM format
            days: List of day names
            exclude_schedule_id: Schedule ID to exclude from conflict check (for updates)
        
        Returns:
            (is_valid, error_message) - error_message is None if valid
        """
        for schedule in self.schedules:
            # Skip if checking against itself (for edits)
            if exclude_schedule_id and schedule.get('id') == exclude_schedule_id:
                continue
            
            # Check if schedules have overlapping days
            existing_days = set(schedule.get('days', []))
            new_days = set(days)
            overlapping_days = existing_days & new_days
            
            # If there are overlapping days and the time is the same
            if overlapping_days and schedule.get('time') == time_str:
                day_list = ', '.join(sorted(overlapping_days, key=lambda d: DAYS_OF_WEEK.index(d)))
                return (False, f"Conflict with schedule '{schedule.get('name', 'Unnamed')}': "
                              f"already runs at {time_str} on {day_list}")
        
        return (True, None)
    
    def add_schedule(self, chime_filename: str, time_str: str, days: List[str], 
                    name: str = "", enabled: bool = True) -> Tuple[bool, str, Optional[int]]:
        """
        Add a new chime schedule.
        
        Args:
            chime_filename: Name of the chime file (from Chimes/ folder)
            time_str: Time in HH:MM format (24-hour)
            days: List of day names (e.g., ['Monday', 'Friday'])
            name: Optional friendly name for the schedule
            enabled: Whether schedule is active
        
        Returns:
            (success, message, schedule_id)
        """
        # Validate time format
        try:
            time_parts = time_str.split(':')
            if len(time_parts) != 2:
                return False, "Time must be in HH:MM format", None
            
            hour = int(time_parts[0])
            minute = int(time_parts[1])
            
            if not (0 <= hour <= 23):
                return False, "Hour must be between 00 and 23", None
            if not (0 <= minute <= 59):
                return False, "Minute must be between 00 and 59", None
            
            # Create datetime.time object to validate
            schedule_time = datetime_time(hour, minute)
            
        except (ValueError, IndexError):
            return False, "Invalid time format. Use HH:MM (e.g., 14:30)", None
        
        # Validate days
        if not days:
            return False, "At least one day must be selected", None
        
        invalid_days = [d for d in days if d not in DAYS_OF_WEEK]
        if invalid_days:
            return False, f"Invalid days: {', '.join(invalid_days)}", None
        
        # Check for conflicts
        is_valid, conflict_error = self.validate_schedule_conflict(time_str, days)
        if not is_valid:
            return False, conflict_error, None
        
        # Generate schedule ID (simple incrementing ID)
        schedule_id = max([s.get('id', 0) for s in self.schedules], default=0) + 1
        
        # Create schedule object
        schedule = {
            'id': schedule_id,
            'name': name or f"Schedule {schedule_id}",
            'chime_filename': chime_filename,
            'time': time_str,
            'days': sorted(days, key=lambda d: DAYS_OF_WEEK.index(d)),  # Sort by weekday order
            'enabled': enabled,
            'created_at': datetime.now().isoformat()
        }
        
        self.schedules.append(schedule)
        
        if self._save_schedules():
            logger.info(f"Added schedule {schedule_id}: {chime_filename} at {time_str} on {', '.join(days)}")
            return True, "Schedule created successfully", schedule_id
        else:
            self.schedules.pop()  # Remove from memory if save failed
            return False, "Failed to save schedule", None
    
    def update_schedule(self, schedule_id: int, **kwargs) -> Tuple[bool, str]:
        """
        Update an existing schedule.
        
        Args:
            schedule_id: ID of schedule to update
            **kwargs: Fields to update (chime_filename, time, days, name, enabled)
        
        Returns:
            (success, message)
        """
        # Find schedule
        schedule = next((s for s in self.schedules if s['id'] == schedule_id), None)
        if not schedule:
            return False, f"Schedule {schedule_id} not found"
        
        # Validate and update fields
        if 'time' in kwargs:
            try:
                time_parts = kwargs['time'].split(':')
                hour, minute = int(time_parts[0]), int(time_parts[1])
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return False, "Invalid time"
                datetime_time(hour, minute)  # Validate
            except (ValueError, IndexError):
                return False, "Invalid time format. Use HH:MM"
        
        if 'days' in kwargs:
            days = kwargs['days']
            if not days:
                return False, "At least one day must be selected"
            invalid_days = [d for d in days if d not in DAYS_OF_WEEK]
            if invalid_days:
                return False, f"Invalid days: {', '.join(invalid_days)}"
            kwargs['days'] = sorted(days, key=lambda d: DAYS_OF_WEEK.index(d))
        
        # Check for conflicts if time or days are being updated
        if 'time' in kwargs or 'days' in kwargs:
            check_time = kwargs.get('time', schedule['time'])
            check_days = kwargs.get('days', schedule['days'])
            is_valid, conflict_error = self.validate_schedule_conflict(
                check_time, check_days, exclude_schedule_id=schedule_id
            )
            if not is_valid:
                return False, conflict_error
        
        # Update schedule
        for key, value in kwargs.items():
            if key in ['chime_filename', 'time', 'days', 'name', 'enabled']:
                schedule[key] = value
        
        schedule['updated_at'] = datetime.now().isoformat()
        
        if self._save_schedules():
            logger.info(f"Updated schedule {schedule_id}")
            return True, "Schedule updated successfully"
        else:
            return False, "Failed to save schedule"
    
    def delete_schedule(self, schedule_id: int) -> Tuple[bool, str]:
        """Delete a schedule."""
        schedule = next((s for s in self.schedules if s['id'] == schedule_id), None)
        if not schedule:
            return False, f"Schedule {schedule_id} not found"
        
        self.schedules = [s for s in self.schedules if s['id'] != schedule_id]
        
        if self._save_schedules():
            logger.info(f"Deleted schedule {schedule_id}")
            return True, "Schedule deleted successfully"
        else:
            return False, "Failed to save changes"
    
    def get_schedule(self, schedule_id: int) -> Optional[Dict]:
        """Get a specific schedule by ID."""
        return next((s for s in self.schedules if s['id'] == schedule_id), None)
    
    def list_schedules(self, enabled_only: bool = False) -> List[Dict]:
        """
        List all schedules.
        
        Args:
            enabled_only: If True, only return enabled schedules
        
        Returns:
            List of schedule dictionaries
        """
        schedules = self.schedules if not enabled_only else [s for s in self.schedules if s.get('enabled', True)]
        # Sort by time
        return sorted(schedules, key=lambda s: s.get('time', '00:00'))
    
    def get_active_chime(self, check_time: Optional[datetime] = None) -> Optional[str]:
        """
        Determine which chime should be active at the given time.
        
        Logic:
        - Finds all enabled schedules that match the current day and have passed
        - If no schedule has passed today, checks yesterday's schedules
        - Returns the chime from the most recent schedule
        - If no matching schedule, returns None (keep current chime)
        
        Args:
            check_time: Time to check (default: now)
        
        Returns:
            Chime filename or None if no schedule applies
        """
        if check_time is None:
            check_time = datetime.now()
        
        current_day = DAYS_OF_WEEK[check_time.weekday()]
        current_time = check_time.time()
        
        # Find all enabled schedules for today that have passed
        matching_schedules = []
        
        for schedule in self.schedules:
            if not schedule.get('enabled', True):
                continue
            
            if current_day not in schedule.get('days', []):
                continue
            
            # Parse schedule time
            try:
                time_parts = schedule['time'].split(':')
                schedule_time = datetime_time(int(time_parts[0]), int(time_parts[1]))
            except (ValueError, IndexError):
                logger.warning(f"Invalid time in schedule {schedule.get('id')}: {schedule.get('time')}")
                continue
            
            # Check if schedule time has passed today
            if current_time >= schedule_time:
                matching_schedules.append({
                    'schedule': schedule,
                    'time': schedule_time,
                    'day_offset': 0  # Today
                })
        
        # If no schedule has passed today, check yesterday's schedules
        if not matching_schedules:
            yesterday_day = DAYS_OF_WEEK[(check_time.weekday() - 1) % 7]
            
            for schedule in self.schedules:
                if not schedule.get('enabled', True):
                    continue
                
                if yesterday_day not in schedule.get('days', []):
                    continue
                
                # Parse schedule time
                try:
                    time_parts = schedule['time'].split(':')
                    schedule_time = datetime_time(int(time_parts[0]), int(time_parts[1]))
                except (ValueError, IndexError):
                    logger.warning(f"Invalid time in schedule {schedule.get('id')}: {schedule.get('time')}")
                    continue
                
                # All of yesterday's schedules have "passed" (they're in the past)
                matching_schedules.append({
                    'schedule': schedule,
                    'time': schedule_time,
                    'day_offset': -1  # Yesterday
                })
        
        if not matching_schedules:
            logger.debug("No matching schedules for current time or yesterday")
            return None
        
        # Find the most recent schedule (latest time that has passed)
        # If multiple schedules from yesterday, pick the latest one
        most_recent = max(matching_schedules, key=lambda x: x['time'])
        chime_filename = most_recent['schedule']['chime_filename']
        
        day_label = "today" if most_recent['day_offset'] == 0 else "yesterday"
        logger.info(f"Active chime at {check_time.strftime('%H:%M')}: {chime_filename} (schedule {most_recent['schedule']['id']} from {day_label})")
        return chime_filename


def get_scheduler(schedule_file=None) -> ChimeScheduler:
    """Get a ChimeScheduler instance."""
    return ChimeScheduler(schedule_file)
