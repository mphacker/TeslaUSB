"""
Chime scheduler service for managing scheduled lock chime changes.

Stores schedules in JSON format with user-friendly time/day selections.
Calculates which chime should be active at any given time.
Supports four schedule types:
- Weekly: Days of week + time
- Date: Specific date (month/day) + time
- Holiday: US Holiday + time
- Recurring: Interval-based rotation (on boot, every X minutes/hours)
"""

import os
import json
import logging
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import GADGET_DIR

logger = logging.getLogger(__name__)

# Schedule storage file
SCHEDULE_FILE = os.path.join(GADGET_DIR, 'chime_schedules.json')

# Days of week (0=Monday, 6=Sunday for Python datetime)
DAYS_OF_WEEK = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

# Recurring schedule intervals (interval_value: display_name)
RECURRING_INTERVALS = {
    'on_boot': 'On every boot/startup',
    '15min': 'Every 15 minutes',
    '30min': 'Every 30 minutes',
    '1hour': 'Every hour',
    '2hour': 'Every 2 hours',
    '4hour': 'Every 4 hours',
    '6hour': 'Every 6 hours',
    '12hour': 'Every 12 hours',
}

# Convert interval to minutes for calculation
INTERVAL_TO_MINUTES = {
    '15min': 15,
    '30min': 30,
    '1hour': 60,
    '2hour': 120,
    '4hour': 240,
    '6hour': 360,
    '12hour': 720,
}

# US Holidays (month, day) - these are fixed-date holidays
US_HOLIDAYS = {
    "New Year's Day": (1, 1),
    "Valentine's Day": (2, 14),
    "St. Patrick's Day": (3, 17),
    "Independence Day": (7, 4),
    "Halloween": (10, 31),
    "Veterans Day": (11, 11),
    "Christmas Eve": (12, 24),
    "Christmas Day": (12, 25),
    "New Year's Eve": (12, 31),
}

# Movable holidays (calculated)
def get_movable_holiday_date(year: int, holiday_name: str) -> Optional[tuple]:
    """Calculate date for movable US holidays."""
    if holiday_name == "Martin Luther King Jr. Day":
        # Third Monday of January
        return _nth_weekday_of_month(year, 1, 0, 3)
    elif holiday_name == "Presidents' Day":
        # Third Monday of February
        return _nth_weekday_of_month(year, 2, 0, 3)
    elif holiday_name == "Memorial Day":
        # Last Monday of May
        return _last_weekday_of_month(year, 5, 0)
    elif holiday_name == "Labor Day":
        # First Monday of September
        return _nth_weekday_of_month(year, 9, 0, 1)
    elif holiday_name == "Columbus Day":
        # Second Monday of October
        return _nth_weekday_of_month(year, 10, 0, 2)
    elif holiday_name == "Thanksgiving":
        # Fourth Thursday of November
        return _nth_weekday_of_month(year, 11, 3, 4)
    elif holiday_name == "Mother's Day":
        # Second Sunday of May
        return _nth_weekday_of_month(year, 5, 6, 2)
    elif holiday_name == "Father's Day":
        # Third Sunday of June
        return _nth_weekday_of_month(year, 6, 6, 3)
    elif holiday_name == "Easter":
        # Calculate Easter using Meeus/Jones/Butcher algorithm
        return _calculate_easter(year)
    return None

def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> tuple:
    """Get the nth occurrence of a weekday in a month (0=Monday, 6=Sunday)."""
    # Start with first day of month
    first_day = datetime(year, month, 1)
    # Find first occurrence of target weekday
    days_ahead = (weekday - first_day.weekday()) % 7
    first_occurrence = first_day + timedelta(days=days_ahead)
    # Add weeks to get nth occurrence
    target_date = first_occurrence + timedelta(weeks=n-1)
    return (target_date.month, target_date.day)

def _last_weekday_of_month(year: int, month: int, weekday: int) -> tuple:
    """Get the last occurrence of a weekday in a month."""
    # Start with last day of month
    if month == 12:
        last_day = datetime(year, 12, 31)
    else:
        last_day = datetime(year, month + 1, 1) - timedelta(days=1)
    
    # Find last occurrence of target weekday
    days_back = (last_day.weekday() - weekday) % 7
    target_date = last_day - timedelta(days=days_back)
    return (target_date.month, target_date.day)

def _calculate_easter(year: int) -> tuple:
    """Calculate Easter Sunday using Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return (month, day)

# Complete list of all holidays
ALL_HOLIDAYS = list(US_HOLIDAYS.keys()) + [
    "Martin Luther King Jr. Day",
    "Presidents' Day", 
    "Easter",
    "Mother's Day",
    "Memorial Day",
    "Father's Day",
    "Labor Day",
    "Columbus Day",
    "Thanksgiving"
]
ALL_HOLIDAYS.sort()


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
    
    def validate_schedule_conflict(self, schedule_type: str, time_str: str, 
                                   days: Optional[List[str]] = None,
                                   month: Optional[int] = None,
                                   day: Optional[int] = None,
                                   holiday: Optional[str] = None,
                                   exclude_schedule_id: Optional[int] = None) -> Tuple[bool, Optional[str]]:
        """
        Check if a schedule conflicts with existing schedules.
        
        Args:
            schedule_type: 'weekly', 'date', or 'holiday'
            time_str: Time in HH:MM format (24-hour)
            days: List of day names (for weekly schedules)
            month: Month number 1-12 (for date schedules)
            day: Day of month (for date schedules)
            holiday: Holiday name (for holiday schedules)
            exclude_schedule_id: Schedule ID to exclude from conflict check (for updates)
        
        Returns:
            (is_valid, error_message) - error_message is None if valid
        """
        for schedule in self.schedules:
            # Skip if checking against itself (for edits)
            if exclude_schedule_id and schedule.get('id') == exclude_schedule_id:
                continue
            
            existing_type = schedule.get('schedule_type', 'weekly')
            existing_time = schedule.get('time')
            
            # Times must match for a conflict
            if existing_time != time_str:
                continue
            
            # Check type-specific conflicts
            if schedule_type == 'weekly' and existing_type == 'weekly':
                # Check if schedules have overlapping days
                existing_days = set(schedule.get('days', []))
                new_days = set(days or [])
                overlapping_days = existing_days & new_days
                
                if overlapping_days:
                    day_list = ', '.join(sorted(overlapping_days, key=lambda d: DAYS_OF_WEEK.index(d)))
                    return (False, f"Conflict with schedule '{schedule.get('name', 'Unnamed')}': "
                                  f"already runs at {time_str} on {day_list}")
            
            elif schedule_type == 'date' and existing_type == 'date':
                # Check if same month/day
                if (schedule.get('month') == month and 
                    schedule.get('day') == day):
                    return (False, f"Conflict with schedule '{schedule.get('name', 'Unnamed')}': "
                                  f"already runs at {time_str} on {month}/{day}")
            
            elif schedule_type == 'holiday' and existing_type == 'holiday':
                # Check if same holiday
                if schedule.get('holiday') == holiday:
                    return (False, f"Conflict with schedule '{schedule.get('name', 'Unnamed')}': "
                                  f"already runs at {time_str} on {holiday}")
        
        return (True, None)
    
    def get_enabled_schedules(self, schedule_type: Optional[str] = None) -> List[Dict]:
        """
        Get all enabled schedules, optionally filtered by type.
        
        Args:
            schedule_type: Optional filter by schedule type ('weekly', 'date', 'holiday', 'recurring')
        
        Returns:
            List of enabled schedule dictionaries
        """
        enabled = [s for s in self.schedules if s.get('enabled', True)]
        if schedule_type:
            enabled = [s for s in enabled if s.get('schedule_type') == schedule_type]
        return enabled
    
    def has_enabled_recurring_schedule(self) -> Tuple[bool, Optional[Dict]]:
        """
        Check if there's an enabled recurring schedule.
        
        Returns:
            (has_recurring, recurring_schedule_dict or None)
        """
        recurring = self.get_enabled_schedules('recurring')
        if recurring:
            return True, recurring[0]
        return False, None
    
    def disable_all_schedules_except(self, exclude_id: Optional[int] = None, exclude_type: Optional[str] = None) -> int:
        """
        Disable all schedules except those matching criteria.
        
        Args:
            exclude_id: Optional schedule ID to exclude from disabling
            exclude_type: Optional schedule type to exclude from disabling
        
        Returns:
            Number of schedules disabled
        """
        disabled_count = 0
        for schedule in self.schedules:
            if not schedule.get('enabled', True):
                continue  # Already disabled
            
            # Skip if matches exclusion criteria
            if exclude_id is not None and schedule['id'] == exclude_id:
                continue
            if exclude_type is not None and schedule.get('schedule_type') == exclude_type:
                continue
            
            schedule['enabled'] = False
            disabled_count += 1
        
        if disabled_count > 0:
            self._save_schedules()
            logger.info(f"Disabled {disabled_count} schedules")
        
        return disabled_count
    
    def add_schedule(self, chime_filename: str, time_str: str = "00:00", 
                    schedule_type: str = 'weekly',
                    days: Optional[List[str]] = None,
                    month: Optional[int] = None,
                    day: Optional[int] = None, 
                    holiday: Optional[str] = None,
                    interval: Optional[str] = None,
                    name: str = "", enabled: bool = True,
                    _skip_conflict_check: bool = False) -> Tuple[bool, str, Optional[int]]:
        """
        Add a new chime schedule.
        
        Args:
            chime_filename: Name of the chime file (from Chimes/ folder) or 'RANDOM'
            time_str: Time in HH:MM format (24-hour) - not used for recurring schedules
            schedule_type: 'weekly', 'date', 'holiday', or 'recurring'
            days: List of day names (for weekly schedules)
            month: Month 1-12 (for date schedules)
            day: Day of month (for date schedules)
            holiday: Holiday name (for holiday schedules)
            interval: Interval value (for recurring schedules) - e.g., '15min', '1hour', 'on_boot'
            name: Optional friendly name for the schedule
            enabled: Whether schedule is active
            _skip_conflict_check: Internal flag to skip mutual exclusivity checks
        
        Returns:
            (success, message, schedule_id)
        """
        # Check for mutual exclusivity between recurring and other schedules
        if enabled and not _skip_conflict_check:
            has_recurring, existing_recurring = self.has_enabled_recurring_schedule()
            
            if schedule_type == 'recurring':
                # Only allow ONE enabled recurring schedule
                if has_recurring:
                    return False, f"A recurring schedule '{existing_recurring['name']}' is already active. Only one recurring schedule can be enabled at a time.", None
                
                # Check if there are other enabled schedules
                other_enabled = [s for s in self.get_enabled_schedules() if s.get('schedule_type') != 'recurring']
                if other_enabled:
                    # This will be handled by the UI with a confirmation dialog
                    # For now, we'll return a special error code that the UI can detect
                    return False, "CONFIRM_DISABLE_OTHERS", None
            
            else:
                # Trying to add a non-recurring schedule while recurring is active
                if has_recurring:
                    return False, f"Cannot add schedule while recurring schedule '{existing_recurring['name']}' is active. Disable the recurring schedule first.", None
        
        # Validate based on schedule type
        if schedule_type == 'recurring':
            # Recurring schedules don't need time validation, but need interval
            if not interval:
                return False, "Interval is required for recurring schedules", None
            
            if interval not in RECURRING_INTERVALS:
                return False, f"Invalid interval: {interval}. Must be one of {list(RECURRING_INTERVALS.keys())}", None
            
            # Recurring schedules should use RANDOM chime
            if chime_filename != 'RANDOM':
                logger.warning(f"Recurring schedule should use RANDOM chime, got {chime_filename}. Forcing to RANDOM.")
                chime_filename = 'RANDOM'
            
            # No conflict checking for recurring schedules (they're time-independent)
            
        else:
            # Validate time format for non-recurring schedules
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
        
        # Validate based on schedule type
        if schedule_type == 'weekly':
            if not days:
                return False, "At least one day must be selected for weekly schedules", None
            
            invalid_days = [d for d in days if d not in DAYS_OF_WEEK]
            if invalid_days:
                return False, f"Invalid days: {', '.join(invalid_days)}", None
            
            # Check for conflicts
            is_valid, conflict_error = self.validate_schedule_conflict(
                schedule_type, time_str, days=days
            )
            if not is_valid:
                return False, conflict_error, None
        
        elif schedule_type == 'date':
            if month is None or day is None:
                return False, "Month and day are required for date schedules", None
            
            if not (1 <= month <= 12):
                return False, "Month must be between 1 and 12", None
            
            if not (1 <= day <= 31):
                return False, "Day must be between 1 and 31", None
            
            # Validate the date is valid for the given month
            try:
                datetime(2024, month, day)  # Use leap year to allow Feb 29
            except ValueError:
                return False, f"Invalid date: {month}/{day}", None
            
            # Check for conflicts
            is_valid, conflict_error = self.validate_schedule_conflict(
                schedule_type, time_str, month=month, day=day
            )
            if not is_valid:
                return False, conflict_error, None
        
        elif schedule_type == 'holiday':
            if not holiday:
                return False, "Holiday is required for holiday schedules", None
            
            if holiday not in ALL_HOLIDAYS:
                return False, f"Invalid holiday: {holiday}", None
            
            # Check for conflicts
            is_valid, conflict_error = self.validate_schedule_conflict(
                schedule_type, time_str, holiday=holiday
            )
            if not is_valid:
                return False, conflict_error, None
        
        elif schedule_type != 'recurring':
            # If it's not weekly, date, holiday, or recurring, it's invalid
            return False, f"Invalid schedule type: {schedule_type}", None
        
        # Generate schedule ID (simple incrementing ID)
        schedule_id = max([s.get('id', 0) for s in self.schedules], default=0) + 1
        
        # Create schedule object
        schedule = {
            'id': schedule_id,
            'name': name or f"Schedule {schedule_id}",
            'chime_filename': chime_filename,
            'schedule_type': schedule_type,
            'enabled': enabled,
            'created_at': datetime.now().isoformat()
        }
        
        # Add time only for non-recurring schedules
        if schedule_type != 'recurring':
            schedule['time'] = time_str
        
        # Add type-specific fields
        if schedule_type == 'weekly':
            schedule['days'] = sorted(days, key=lambda d: DAYS_OF_WEEK.index(d))
        elif schedule_type == 'date':
            schedule['month'] = month
            schedule['day'] = day
        elif schedule_type == 'holiday':
            schedule['holiday'] = holiday
        elif schedule_type == 'recurring':
            schedule['interval'] = interval
        
        self.schedules.append(schedule)
        
        if self._save_schedules():
            logger.info(f"Added {schedule_type} schedule {schedule_id}: {chime_filename} at {time_str}")
            return True, "Schedule created successfully", schedule_id
        else:
            self.schedules.pop()  # Remove from memory if save failed
            return False, "Failed to save schedule", None
    
    def update_schedule(self, schedule_id: int, **kwargs) -> Tuple[bool, str]:
        """
        Update an existing schedule.
        
        Args:
            schedule_id: ID of schedule to update
            **kwargs: Fields to update (chime_filename, time, schedule_type, days, month, day, holiday, name, enabled)
        
        Returns:
            (success, message)
        """
        # Find schedule
        schedule = next((s for s in self.schedules if s['id'] == schedule_id), None)
        if not schedule:
            return False, f"Schedule {schedule_id} not found"
        
        # Check for enable conflicts BEFORE making any changes
        if 'enabled' in kwargs and kwargs['enabled'] == True:
            current_type = schedule.get('schedule_type')
            was_disabled = not schedule.get('enabled', True)
            
            # Only check conflicts if we're enabling a previously disabled schedule
            if was_disabled:
                has_recurring, existing_recurring = self.has_enabled_recurring_schedule()
                
                if current_type == 'recurring':
                    # Trying to enable a recurring schedule
                    if has_recurring and existing_recurring['id'] != schedule_id:
                        return False, f"Cannot enable: recurring schedule '{existing_recurring['name']}' is already active"
                    
                    # Check if other schedules are enabled
                    other_enabled = [s for s in self.get_enabled_schedules() if s.get('schedule_type') != 'recurring']
                    if other_enabled:
                        return False, "CONFIRM_DISABLE_OTHERS"
                
                else:
                    # Trying to enable a non-recurring schedule
                    if has_recurring:
                        return False, f"Cannot enable while recurring schedule '{existing_recurring['name']}' is active"
        
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
        
        # Get effective schedule type
        schedule_type = kwargs.get('schedule_type', schedule.get('schedule_type', 'weekly'))
        
        # Validate type-specific fields
        if schedule_type == 'recurring':
            if 'interval' in kwargs:
                interval = kwargs['interval']
                if interval not in RECURRING_INTERVALS:
                    return False, f"Invalid interval: {interval}"
            # Recurring schedules should use RANDOM chime
            if 'chime_filename' in kwargs and kwargs['chime_filename'] != 'RANDOM':
                logger.warning(f"Recurring schedule should use RANDOM chime. Forcing to RANDOM.")
                kwargs['chime_filename'] = 'RANDOM'
        
        elif schedule_type == 'weekly':
            if 'days' in kwargs:
                days = kwargs['days']
                if not days:
                    return False, "At least one day must be selected"
                invalid_days = [d for d in days if d not in DAYS_OF_WEEK]
                if invalid_days:
                    return False, f"Invalid days: {', '.join(invalid_days)}"
                kwargs['days'] = sorted(days, key=lambda d: DAYS_OF_WEEK.index(d))
        
        elif schedule_type == 'date':
            if 'month' in kwargs or 'day' in kwargs:
                month = kwargs.get('month', schedule.get('month'))
                day = kwargs.get('day', schedule.get('day'))
                
                if month is None or day is None:
                    return False, "Month and day are required"
                
                if not (1 <= month <= 12):
                    return False, "Month must be between 1 and 12"
                
                if not (1 <= day <= 31):
                    return False, "Day must be between 1 and 31"
                
                try:
                    datetime(2024, month, day)
                except ValueError:
                    return False, f"Invalid date: {month}/{day}"
        
        elif schedule_type == 'holiday':
            if 'holiday' in kwargs:
                holiday = kwargs['holiday']
                if holiday not in ALL_HOLIDAYS:
                    return False, f"Invalid holiday: {holiday}"
        
        # Check for conflicts if relevant fields are being updated
        if any(k in kwargs for k in ['time', 'schedule_type', 'days', 'month', 'day', 'holiday']):
            check_time = kwargs.get('time', schedule['time'])
            check_type = schedule_type
            
            # Prepare conflict check parameters
            conflict_params = {
                'schedule_type': check_type,
                'time_str': check_time,
                'exclude_schedule_id': schedule_id
            }
            
            if check_type == 'weekly':
                conflict_params['days'] = kwargs.get('days', schedule.get('days', []))
            elif check_type == 'date':
                conflict_params['month'] = kwargs.get('month', schedule.get('month'))
                conflict_params['day'] = kwargs.get('day', schedule.get('day'))
            elif check_type == 'holiday':
                conflict_params['holiday'] = kwargs.get('holiday', schedule.get('holiday'))
            
            is_valid, conflict_error = self.validate_schedule_conflict(**conflict_params)
            if not is_valid:
                return False, conflict_error
        
        # Update schedule
        valid_keys = ['chime_filename', 'time', 'schedule_type', 'days', 'month', 'day', 'holiday', 'interval', 'name', 'enabled']
        for key, value in kwargs.items():
            if key in valid_keys:
                schedule[key] = value
        
        # Clear last_run if schedule timing changed (time, days, date, holiday, or interval)
        # This allows the schedule to run again even if it already ran today
        if any(k in kwargs for k in ['time', 'schedule_type', 'days', 'month', 'day', 'holiday', 'interval']):
            schedule.pop('last_run', None)
            logger.info(f"Cleared last_run for schedule {schedule_id} due to timing change")
        
        # If schedule type changed, remove old type-specific fields
        if 'schedule_type' in kwargs:
            if schedule_type == 'weekly':
                schedule.pop('month', None)
                schedule.pop('day', None)
                schedule.pop('holiday', None)
                schedule.pop('interval', None)
            elif schedule_type == 'date':
                schedule.pop('days', None)
                schedule.pop('holiday', None)
                schedule.pop('interval', None)
            elif schedule_type == 'holiday':
                schedule.pop('days', None)
                schedule.pop('month', None)
                schedule.pop('day', None)
                schedule.pop('interval', None)
            elif schedule_type == 'recurring':
                schedule.pop('days', None)
                schedule.pop('month', None)
                schedule.pop('day', None)
                schedule.pop('holiday', None)
                schedule.pop('time', None)  # Recurring doesn't need time
        
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
    
    def add_recurring_schedule_with_disable(self, chime_filename: str, interval: str, 
                                           name: str = "", enabled: bool = True) -> Tuple[bool, str, Optional[int], int]:
        """
        Add a recurring schedule and disable all other schedules.
        
        Args:
            chime_filename: Name of the chime file (should be 'RANDOM')
            interval: Interval value - e.g., '15min', '1hour', 'on_boot'
            name: Optional friendly name for the schedule
            enabled: Whether schedule is active
        
        Returns:
            (success, message, schedule_id, num_disabled)
        """
        # Disable all other schedules first
        num_disabled = self.disable_all_schedules_except(exclude_type='recurring')
        
        # Now add the recurring schedule (bypassing the conflict check)
        success, message, schedule_id = self.add_schedule(
            chime_filename=chime_filename,
            schedule_type='recurring',
            interval=interval,
            name=name,
            enabled=enabled,
            _skip_conflict_check=True  # Skip check since we already disabled others
        )
        
        return success, message, schedule_id, num_disabled
    
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
        
        Precedence order (highest to lowest):
        1. Holiday schedules - if a holiday is today, use its chime for the entire day
        2. Specific date schedules - if a date schedule matches and time has passed
        3. Weekly day schedules - default fallback for recurring weekly schedules
        
        Logic:
        - Evaluates all enabled schedules (weekly, date, and holiday)
        - For each type, finds schedules matching current day that have passed
        - If no schedule has passed today, checks yesterday's schedules
        - Returns the chime from the most recent schedule of the highest priority type
        - If no matching schedule, returns None (keep current chime)
        
        Args:
            check_time: Time to check (default: now)
        
        Returns:
            Chime filename or None if no schedule applies
        """
        if check_time is None:
            check_time = datetime.now()
        
        current_day_name = DAYS_OF_WEEK[check_time.weekday()]
        current_month = check_time.month
        current_day = check_time.day
        current_time = check_time.time()
        
        # Get today's holidays
        today_holidays = self._get_holidays_for_date(check_time.year, current_month, current_day)
        
        # Separate schedules by type
        holiday_schedules = []
        date_schedules = []
        weekly_schedules = []
        
        for schedule in self.schedules:
            if not schedule.get('enabled', True):
                continue
            
            schedule_type = schedule.get('schedule_type', 'weekly')
            
            # Parse schedule time
            try:
                time_parts = schedule['time'].split(':')
                schedule_time = datetime_time(int(time_parts[0]), int(time_parts[1]))
            except (ValueError, IndexError, KeyError):
                logger.warning(f"Invalid time in schedule {schedule.get('id')}: {schedule.get('time')}")
                continue
            
            # Categorize by type and check if matches today
            if schedule_type == 'holiday':
                if schedule.get('holiday') in today_holidays:
                    # Holiday schedules apply for entire day, check if time has passed
                    if current_time >= schedule_time:
                        holiday_schedules.append({
                            'schedule': schedule,
                            'time': schedule_time,
                            'day_offset': 0
                        })
            
            elif schedule_type == 'date':
                if (schedule.get('month') == current_month and 
                    schedule.get('day') == current_day):
                    # Date schedule matches today
                    if current_time >= schedule_time:
                        date_schedules.append({
                            'schedule': schedule,
                            'time': schedule_time,
                            'day_offset': 0
                        })
            
            elif schedule_type == 'weekly':
                if current_day_name in schedule.get('days', []):
                    # Weekly schedule matches today
                    if current_time >= schedule_time:
                        weekly_schedules.append({
                            'schedule': schedule,
                            'time': schedule_time,
                            'day_offset': 0
                        })
        
        # If no schedules have passed today, check yesterday's schedules
        if not holiday_schedules and not date_schedules and not weekly_schedules:
            yesterday = check_time - timedelta(days=1)
            yesterday_day_name = DAYS_OF_WEEK[yesterday.weekday()]
            yesterday_month = yesterday.month
            yesterday_day = yesterday.day
            yesterday_holidays = self._get_holidays_for_date(yesterday.year, yesterday_month, yesterday_day)
            
            for schedule in self.schedules:
                if not schedule.get('enabled', True):
                    continue
                
                schedule_type = schedule.get('schedule_type', 'weekly')
                
                # Parse schedule time
                try:
                    time_parts = schedule['time'].split(':')
                    schedule_time = datetime_time(int(time_parts[0]), int(time_parts[1]))
                except (ValueError, IndexError, KeyError):
                    continue
                
                # Check if schedule matches yesterday
                if schedule_type == 'holiday':
                    if schedule.get('holiday') in yesterday_holidays:
                        holiday_schedules.append({
                            'schedule': schedule,
                            'time': schedule_time,
                            'day_offset': -1
                        })
                
                elif schedule_type == 'date':
                    if (schedule.get('month') == yesterday_month and 
                        schedule.get('day') == yesterday_day):
                        date_schedules.append({
                            'schedule': schedule,
                            'time': schedule_time,
                            'day_offset': -1
                        })
                
                elif schedule_type == 'weekly':
                    if yesterday_day_name in schedule.get('days', []):
                        weekly_schedules.append({
                            'schedule': schedule,
                            'time': schedule_time,
                            'day_offset': -1
                        })
        
        # Apply precedence: Holiday > Date > Weekly
        # Within each type, use the most recent (latest time)
        most_recent = None
        schedule_type_used = None
        
        if holiday_schedules:
            most_recent = max(holiday_schedules, key=lambda x: x['time'])
            schedule_type_used = 'holiday'
        elif date_schedules:
            most_recent = max(date_schedules, key=lambda x: x['time'])
            schedule_type_used = 'date'
        elif weekly_schedules:
            most_recent = max(weekly_schedules, key=lambda x: x['time'])
            schedule_type_used = 'weekly'
        
        if not most_recent:
            logger.debug("No matching schedules for current time or yesterday")
            return None
        
        chime_filename = most_recent['schedule']['chime_filename']
        day_label = "today" if most_recent['day_offset'] == 0 else "yesterday"
        
        # Handle random chime selection
        if chime_filename == 'RANDOM':
            random_chime = self._select_random_chime()
            if random_chime:
                logger.info(f"Active chime at {check_time.strftime('%H:%M')}: {random_chime} "
                           f"(randomly selected from {schedule_type_used} schedule {most_recent['schedule']['id']} from {day_label})")
                return random_chime
            else:
                logger.warning(f"Random chime requested but no valid chimes found in library")
                return None
        
        logger.info(f"Active chime at {check_time.strftime('%H:%M')}: {chime_filename} "
                   f"({schedule_type_used} schedule {most_recent['schedule']['id']} from {day_label})")
        return chime_filename
    
    def _select_random_chime(self, exclude_current: bool = True) -> Optional[str]:
        """
        Select a random chime from the Chimes library.
        
        Args:
            exclude_current: If True, excludes the currently active LockChime.wav from selection
        
        Returns:
            Random chime filename or None if no valid chimes found
        """
        import random
        from config import CHIMES_FOLDER, LOCK_CHIME_FILENAME
        from services.partition_service import get_mount_path
        from services.lock_chime_service import validate_tesla_wav
        
        # Get part2 mount path
        part2_mount = get_mount_path('part2')
        if not part2_mount:
            logger.error("Cannot select random chime: part2 not mounted")
            return None
        
        # Get currently active chime filename if we should exclude it
        current_chime = None
        if exclude_current:
            active_chime_path = os.path.join(part2_mount, LOCK_CHIME_FILENAME)
            if os.path.isfile(active_chime_path):
                # Check Chimes library to find matching file (by content hash or size)
                # For simplicity, we'll compare file size and assume matching size = same file
                current_size = os.path.getsize(active_chime_path)
                
                chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
                if os.path.isdir(chimes_dir):
                    try:
                        for entry in os.listdir(chimes_dir):
                            if not entry.lower().endswith('.wav'):
                                continue
                            entry_path = os.path.join(chimes_dir, entry)
                            if os.path.isfile(entry_path):
                                if os.path.getsize(entry_path) == current_size:
                                    # Likely the same file - exclude it
                                    current_chime = entry
                                    logger.info(f"Identified current active chime as: {current_chime}")
                                    break
                    except OSError:
                        pass
        
        chimes_dir = os.path.join(part2_mount, CHIMES_FOLDER)
        if not os.path.isdir(chimes_dir):
            logger.error(f"Chimes directory not found: {chimes_dir}")
            return None
        
        # Get all valid WAV files
        valid_chimes = []
        try:
            for entry in os.listdir(chimes_dir):
                if not entry.lower().endswith('.wav'):
                    continue
                
                # Skip current chime if excluding
                if exclude_current and current_chime and entry == current_chime:
                    logger.info(f"Excluding current active chime from random selection: {entry}")
                    continue
                
                full_path = os.path.join(chimes_dir, entry)
                if os.path.isfile(full_path):
                    # Validate the chime
                    is_valid, _ = validate_tesla_wav(full_path)
                    if is_valid:
                        valid_chimes.append(entry)
        except OSError as e:
            logger.error(f"Error reading chimes directory: {e}")
            return None
        
        if not valid_chimes:
            # If no valid chimes after excluding current, try including current
            if exclude_current and current_chime:
                logger.warning("No other valid chimes found, will include current chime")
                return self._select_random_chime(exclude_current=False)
            logger.warning("No valid chimes found in library")
            return None
        
        # Select random chime
        selected = random.choice(valid_chimes)
        logger.info(f"Randomly selected chime: {selected} from {len(valid_chimes)} valid chimes")
        return selected
    
    def _should_execute_recurring(self, schedule: Dict, check_time: datetime) -> Tuple[bool, Optional[str], str]:
        """
        Determine if a recurring schedule should execute based on interval.
        
        Args:
            schedule: The recurring schedule dictionary
            check_time: Current time to check against
        
        Returns:
            (should_execute, chime_filename, reason)
        """
        interval = schedule.get('interval')
        if not interval:
            return False, None, "No interval specified for recurring schedule"
        
        # Check if schedule has run before
        last_run = schedule.get('last_run')
        if not last_run:
            # Never run before - execute now (including on_boot)
            chime_filename = schedule.get('chime_filename', 'RANDOM')
            return True, chime_filename, "Recurring schedule never executed before"
        
        # For 'on_boot' interval, check if last_run was before system boot time
        if interval == 'on_boot':
            try:
                # Get system boot time
                with open('/proc/uptime', 'r') as f:
                    uptime_seconds = float(f.read().split()[0])
                boot_time = check_time - timedelta(seconds=uptime_seconds)
                
                # Parse last_run timestamp
                last_run_dt = datetime.fromisoformat(last_run)
                
                # If last_run was before boot, execute now
                if last_run_dt < boot_time:
                    chime_filename = schedule.get('chime_filename', 'RANDOM')
                    return True, chime_filename, f"On-boot: Last run ({last_run_dt.strftime('%Y-%m-%d %H:%M:%S')}) was before current boot ({boot_time.strftime('%Y-%m-%d %H:%M:%S')})"
                else:
                    return False, None, f"On-boot schedule already executed this boot session (ran at {last_run_dt.strftime('%H:%M:%S')})"
                    
            except (OSError, ValueError, IndexError) as e:
                logger.warning(f"Error checking boot time for on_boot schedule: {e}")
                # If we can't determine boot time, don't execute (safer to skip than duplicate)
                return False, None, f"Unable to determine boot time: {e}"
        
        try:
            last_run_dt = datetime.fromisoformat(last_run)
            
            # Get interval in minutes
            interval_minutes = INTERVAL_TO_MINUTES.get(interval)
            if interval_minutes is None:
                return False, None, f"Invalid interval: {interval}"
            
            # Calculate time since last run
            time_since_last = (check_time - last_run_dt).total_seconds() / 60
            
            if time_since_last >= interval_minutes:
                # Enough time has passed
                chime_filename = schedule.get('chime_filename', 'RANDOM')
                return True, chime_filename, f"Interval {RECURRING_INTERVALS[interval]} elapsed (last run: {last_run_dt.strftime('%H:%M:%S')})"
            else:
                remaining = interval_minutes - time_since_last
                return False, None, f"Only {time_since_last:.0f} minutes since last run, need {interval_minutes} ({remaining:.0f} min remaining)"
                
        except ValueError:
            logger.warning(f"Invalid last_run timestamp for recurring schedule {schedule.get('id')}: {last_run}")
            # If timestamp is invalid, allow execution
            chime_filename = schedule.get('chime_filename', 'RANDOM')
            return True, chime_filename, "Invalid last_run timestamp, executing now"
    
    def should_execute_schedule(self, schedule_id: int, check_time: Optional[datetime] = None) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Determine if a schedule should be executed now.
        
        This checks if:
        1. The schedule is currently active (time/day/date matches)
        2. The schedule hasn't been run yet for this occurrence
        
        Args:
            schedule_id: ID of the schedule to check
            check_time: Time to check (default: now)
        
        Returns:
            (should_execute, chime_filename, reason)
            - should_execute: True if schedule should run now
            - chime_filename: The chime to use (may be 'RANDOM')
            - reason: Human-readable reason for the decision
        """
        if check_time is None:
            check_time = datetime.now()
        
        # Find the schedule
        schedule = next((s for s in self.schedules if s['id'] == schedule_id), None)
        if not schedule:
            return False, None, f"Schedule {schedule_id} not found"
        
        if not schedule.get('enabled', True):
            return False, None, "Schedule is disabled"
        
        schedule_type = schedule.get('schedule_type', 'weekly')
        
        # Handle recurring schedules differently (interval-based, not time-based)
        if schedule_type == 'recurring':
            return self._should_execute_recurring(schedule, check_time)
        
        # Parse schedule time (not needed for recurring)
        try:
            time_parts = schedule['time'].split(':')
            schedule_time = datetime_time(int(time_parts[0]), int(time_parts[1]))
        except (ValueError, IndexError, KeyError):
            return False, None, f"Invalid time format: {schedule.get('time')}"
        
        current_time = check_time.time()
        current_day_name = DAYS_OF_WEEK[check_time.weekday()]
        current_month = check_time.month
        current_day = check_time.day
        
        # Check if schedule matches current time/day
        matches_today = False
        
        if schedule_type == 'weekly':
            if current_day_name in schedule.get('days', []):
                matches_today = True
        elif schedule_type == 'date':
            if (schedule.get('month') == current_month and 
                schedule.get('day') == current_day):
                matches_today = True
        elif schedule_type == 'holiday':
            today_holidays = self._get_holidays_for_date(check_time.year, current_month, current_day)
            if schedule.get('holiday') in today_holidays:
                matches_today = True
        
        if not matches_today:
            return False, None, "Schedule doesn't match today"
        
        # Check if we've already run this schedule today (check this BEFORE time comparison)
        # This prevents re-running if device was offline during scheduled time
        last_run = schedule.get('last_run')
        if last_run:
            try:
                last_run_dt = datetime.fromisoformat(last_run)
                
                # If last run was today, don't run again regardless of time
                # This is the key: if we already ran today, we're done
                if last_run_dt.date() == check_time.date():
                    return False, None, f"Already ran today at {last_run_dt.strftime('%H:%M:%S')}"
            except ValueError:
                logger.warning(f"Invalid last_run timestamp for schedule {schedule_id}: {last_run}")
        
        # Now check if the scheduled time has passed
        # We allow execution at any time after the scheduled time (within the same day)
        # as long as we haven't already run today (checked above)
        if current_time < schedule_time:
            return False, None, f"Scheduled time {schedule['time']} hasn't arrived yet (current: {current_time.strftime('%H:%M')})"
        
        # Should execute!
        chime_filename = schedule['chime_filename']
        return True, chime_filename, f"Schedule should run (time: {schedule['time']}, last run: {last_run or 'never'})"
    
    def record_execution(self, schedule_id: int, execution_time: Optional[datetime] = None) -> bool:
        """
        Record that a schedule was executed.
        
        Args:
            schedule_id: ID of the schedule that was executed
            execution_time: Time of execution (default: now)
        
        Returns:
            True if successfully recorded, False otherwise
        """
        if execution_time is None:
            execution_time = datetime.now()
        
        # Find and update the schedule
        schedule = next((s for s in self.schedules if s['id'] == schedule_id), None)
        if not schedule:
            logger.error(f"Cannot record execution: schedule {schedule_id} not found")
            return False
        
        # Update last_run timestamp
        schedule['last_run'] = execution_time.isoformat()
        
        # Save schedules
        if self._save_schedules():
            logger.info(f"Recorded execution of schedule {schedule_id} at {execution_time.isoformat()}")
            return True
        else:
            logger.error(f"Failed to save execution record for schedule {schedule_id}")
            return False
    
    def _get_holidays_for_date(self, year: int, month: int, day: int) -> List[str]:
        """
        Get list of holidays that fall on the given date.
        
        Args:
            year: Year
            month: Month (1-12)
            day: Day of month
        
        Returns:
            List of holiday names
        """
        holidays = []
        
        # Check fixed holidays
        for holiday_name, (h_month, h_day) in US_HOLIDAYS.items():
            if h_month == month and h_day == day:
                holidays.append(holiday_name)
        
        # Check movable holidays
        movable_holiday_names = [
            "Martin Luther King Jr. Day",
            "Presidents' Day",
            "Easter",
            "Mother's Day",
            "Memorial Day",
            "Father's Day",
            "Labor Day",
            "Columbus Day",
            "Thanksgiving"
        ]
        
        for holiday_name in movable_holiday_names:
            holiday_date = get_movable_holiday_date(year, holiday_name)
            if holiday_date and holiday_date[0] == month and holiday_date[1] == day:
                holidays.append(holiday_name)
        
        return holidays


def cleanup_expired_date_schedules(scheduler: ChimeScheduler, check_time: Optional[datetime] = None) -> int:
    """
    Delete date-specific schedules that have passed and already executed.
    
    Only removes schedules where:
    1. schedule_type == 'date'
    2. The date/time has passed (current time > scheduled date/time)
    3. The schedule has been executed (has a last_run timestamp)
    
    Args:
        scheduler: ChimeScheduler instance
        check_time: Time to check against (default: now)
    
    Returns:
        Number of schedules deleted
    """
    if check_time is None:
        check_time = datetime.now()
    
    schedules_to_delete = []
    
    for schedule in scheduler.schedules:
        # Only process date-specific schedules
        if schedule.get('schedule_type') != 'date':
            continue
        
        # Check if it has been executed
        last_run = schedule.get('last_run')
        if not last_run:
            continue  # Not executed yet, keep it
        
        # Parse the scheduled date/time
        try:
            month = schedule.get('month')
            day = schedule.get('day')
            time_str = schedule.get('time', '00:00')
            
            if not month or not day:
                continue
            
            time_parts = time_str.split(':')
            hour = int(time_parts[0])
            minute = int(time_parts[1])
            
            # Use current year for comparison
            scheduled_dt = datetime(check_time.year, month, day, hour, minute)
            
            # If the scheduled time has passed, mark for deletion
            if check_time > scheduled_dt:
                schedules_to_delete.append(schedule['id'])
                logger.info(f"Marking expired date schedule for deletion: {schedule.get('name', 'Unnamed')} "
                           f"({month}/{day} at {time_str}, last run: {last_run})")
        
        except (ValueError, IndexError, KeyError) as e:
            logger.warning(f"Error parsing schedule {schedule.get('id')}: {e}")
            continue
    
    # Delete marked schedules
    deleted_count = 0
    for schedule_id in schedules_to_delete:
        if scheduler.delete_schedule(schedule_id):
            deleted_count += 1
    
    if deleted_count > 0:
        logger.info(f"Cleaned up {deleted_count} expired date schedule(s)")
    
    return deleted_count


def get_scheduler(schedule_file=None) -> ChimeScheduler:
    """Get a ChimeScheduler instance."""
    return ChimeScheduler(schedule_file)


def get_holidays_list() -> List[str]:
    """Get sorted list of all US holidays."""
    return ALL_HOLIDAYS.copy()


def get_recurring_intervals() -> Dict[str, str]:
    """Get dictionary of recurring interval values and display names."""
    return RECURRING_INTERVALS.copy()


def get_holidays_with_dates(year: int = None) -> List[Dict[str, any]]:
    """
    Get list of all US holidays with their dates for a specific year.
    
    Args:
        year: Year to calculate dates for (default: current year)
    
    Returns:
        List of dicts with 'name', 'month', 'day' keys
    """
    if year is None:
        year = datetime.now().year
    
    holidays_with_dates = []
    
    # Add fixed holidays
    for holiday_name, (month, day) in US_HOLIDAYS.items():
        holidays_with_dates.append({
            'name': holiday_name,
            'month': month,
            'day': day
        })
    
    # Add movable holidays
    movable_holiday_names = [
        "Martin Luther King Jr. Day",
        "Presidents' Day",
        "Easter",
        "Mother's Day",
        "Memorial Day",
        "Father's Day",
        "Labor Day",
        "Columbus Day",
        "Thanksgiving"
    ]
    
    for holiday_name in movable_holiday_names:
        date = get_movable_holiday_date(year, holiday_name)
        if date:
            month, day = date
            holidays_with_dates.append({
                'name': holiday_name,
                'month': month,
                'day': day
            })
    
    # Sort by date (month, then day)
    holidays_with_dates.sort(key=lambda h: (h['month'], h['day']))
    
    return holidays_with_dates


def format_schedule_display(schedule: Dict) -> str:
    """
    Format a schedule for display.
    
    Args:
        schedule: Schedule dictionary
    
    Returns:
        Formatted string describing when the schedule runs
    """
    schedule_type = schedule.get('schedule_type', 'weekly')
    
    # Recurring schedules don't have time, just interval
    if schedule_type == 'recurring':
        interval = schedule.get('interval', 'unknown')
        interval_display = RECURRING_INTERVALS.get(interval, interval)
        return interval_display
    
    time_str = schedule.get('time', '00:00')
    
    # Convert 24-hour time to 12-hour format
    try:
        time_parts = time_str.split(':')
        hour = int(time_parts[0])
        minute = int(time_parts[1])
        
        am_pm = 'AM' if hour < 12 else 'PM'
        display_hour = hour % 12
        if display_hour == 0:
            display_hour = 12
        
        time_12h = f"{display_hour}:{minute:02d} {am_pm}"
    except (ValueError, IndexError):
        time_12h = time_str
    
    if schedule_type == 'weekly':
        days = schedule.get('days', [])
        return f"{', '.join(days)} at {time_12h}"
    
    elif schedule_type == 'date':
        month = schedule.get('month', 1)
        day = schedule.get('day', 1)
        return f"{month}/{day} at {time_12h}"
    
    elif schedule_type == 'holiday':
        holiday = schedule.get('holiday', 'Unknown')
        return f"{holiday} at {time_12h}"
    
    return "Unknown schedule type"


def format_last_run(last_run_iso: str) -> str:
    """
    Format the last run timestamp for display.
    
    Args:
        last_run_iso: ISO format timestamp string
    
    Returns:
        Human-readable string like "Today at 3:00 PM" or "Nov 9 at 8:00 AM"
    """
    try:
        from datetime import datetime
        
        last_run_dt = datetime.fromisoformat(last_run_iso)
        now = datetime.now()
        
        # Format time as 12-hour
        hour = last_run_dt.hour
        minute = last_run_dt.minute
        am_pm = 'AM' if hour < 12 else 'PM'
        display_hour = hour % 12
        if display_hour == 0:
            display_hour = 12
        time_str = f"{display_hour}:{minute:02d} {am_pm}"
        
        # Check if it's today
        if last_run_dt.date() == now.date():
            return f"Today at {time_str}"
        
        # Check if it's yesterday
        from datetime import timedelta
        yesterday = now.date() - timedelta(days=1)
        if last_run_dt.date() == yesterday:
            return f"Yesterday at {time_str}"
        
        # Otherwise show date
        month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 
                      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        month_str = month_names[last_run_dt.month - 1]
        return f"{month_str} {last_run_dt.day} at {time_str}"
        
    except (ValueError, AttributeError) as e:
        return last_run_iso  # Fallback to raw value


