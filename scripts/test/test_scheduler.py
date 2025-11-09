#!/usr/bin/env python3
"""Quick test script for the enhanced chime scheduler."""

import sys
from pathlib import Path
from datetime import datetime

# Add scripts/web to path
SCRIPT_DIR = Path(__file__).parent.resolve()
WEB_DIR = SCRIPT_DIR.parent / 'web'  # Go up one level to scripts, then into web
sys.path.insert(0, str(WEB_DIR))

from services.chime_scheduler_service import (
    get_holidays_list,
    get_movable_holiday_date,
    US_HOLIDAYS,
    format_schedule_display
)

def test_holidays():
    """Test holiday date calculations."""
    print("=" * 60)
    print("Testing US Holiday Calculations for 2024")
    print("=" * 60)
    
    # Test fixed holidays
    print("\nFixed Holidays:")
    for holiday_name, (month, day) in sorted(US_HOLIDAYS.items()):
        print(f"  {holiday_name:25} {month}/{day}")
    
    # Test movable holidays
    print("\nMovable Holidays (2024):")
    movable_holidays = [
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
    
    for holiday_name in movable_holidays:
        date = get_movable_holiday_date(2024, holiday_name)
        if date:
            month, day = date
            date_obj = datetime(2024, month, day)
            day_name = date_obj.strftime('%A')
            print(f"  {holiday_name:30} {month}/{day} ({day_name})")
    
    # Test 2025 too
    print("\nMovable Holidays (2025):")
    for holiday_name in movable_holidays:
        date = get_movable_holiday_date(2025, holiday_name)
        if date:
            month, day = date
            date_obj = datetime(2025, month, day)
            day_name = date_obj.strftime('%A')
            print(f"  {holiday_name:30} {month}/{day} ({day_name})")
    
    # Test all holidays list
    print(f"\nTotal holidays available: {len(get_holidays_list())}")
    print("All holidays:", ", ".join(get_holidays_list()[:5]), "...")


def test_schedule_formatting():
    """Test schedule display formatting."""
    print("\n" + "=" * 60)
    print("Testing Schedule Display Formatting")
    print("=" * 60)
    
    test_schedules = [
        {
            'schedule_type': 'weekly',
            'time': '07:30',
            'days': ['Monday', 'Wednesday', 'Friday']
        },
        {
            'schedule_type': 'date',
            'time': '00:01',
            'month': 12,
            'day': 25
        },
        {
            'schedule_type': 'holiday',
            'time': '00:01',
            'holiday': "New Year's Day"
        },
        {
            'schedule_type': 'weekly',
            'time': '17:00',
            'days': ['Saturday', 'Sunday']
        },
        {
            'schedule_type': 'weekly',
            'time': '12:00',
            'days': ['Tuesday']
        }
    ]
    
    for schedule in test_schedules:
        display = format_schedule_display(schedule)
        print(f"  {schedule['schedule_type']:10} → {display}")


if __name__ == '__main__':
    test_holidays()
    test_schedule_formatting()
    print("\n" + "=" * 60)
    print("✅ All tests completed!")
    print("=" * 60)
