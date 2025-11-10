#!/usr/bin/env python3
"""
Test script for chime scheduler precedence logic.

Tests that the correct chime is selected based on priority:
1. Holiday schedules (highest)
2. Specific date schedules (medium)
3. Weekly day schedules (lowest)
"""

import sys
import os
import tempfile
import logging
from pathlib import Path
from datetime import datetime, time as datetime_time

# Add scripts/web to path
SCRIPT_DIR = Path(__file__).parent.resolve()
WEB_DIR = SCRIPT_DIR.parent / 'web'  # Go up one level to scripts, then into web
sys.path.insert(0, str(WEB_DIR))

# Suppress expected error messages from loading empty temp files
logging.getLogger('services.chime_scheduler_service').setLevel(logging.CRITICAL)

from services.chime_scheduler_service import ChimeScheduler


class TestSchedulePrecedence:
    """Test suite for schedule precedence logic."""
    
    def __init__(self):
        self.test_results = []
        self.temp_file = None
        
    def setup(self):
        """Create temporary schedule file for testing."""
        # Create a temporary file for schedules
        fd, self.temp_file = tempfile.mkstemp(suffix='.json', prefix='test_schedules_')
        # Write empty JSON array to avoid parse errors
        os.write(fd, b'[]')
        os.close(fd)
        return ChimeScheduler(self.temp_file)
    
    def teardown(self):
        """Clean up temporary files."""
        if self.temp_file and os.path.exists(self.temp_file):
            os.remove(self.temp_file)
    
    def log_test(self, test_name, passed, expected, actual, details=""):
        """Log test result."""
        status = "✅ PASS" if passed else "❌ FAIL"
        self.test_results.append({
            'name': test_name,
            'passed': passed,
            'expected': expected,
            'actual': actual,
            'details': details
        })
        print(f"{status} {test_name}")
        if not passed:
            print(f"   Expected: {expected}")
            print(f"   Actual:   {actual}")
            if details:
                print(f"   Details:  {details}")
    
    def test_weekly_schedule_only(self):
        """Test that weekly schedule works when it's the only type."""
        scheduler = self.setup()
        
        # Add weekly schedule: Monday at 8:00 AM
        scheduler.add_schedule(
            chime_filename='weekly_monday.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Monday'],
            name='Monday Morning',
            enabled=True
        )
        
        # Test on Monday at 9:00 AM (after 8:00 AM)
        # November 10, 2025 is a Monday
        test_time = datetime(2025, 11, 10, 9, 0)
        active_chime = scheduler.get_active_chime(test_time)
        
        self.log_test(
            "Weekly Schedule Only",
            active_chime == 'weekly_monday.wav',
            'weekly_monday.wav',
            active_chime,
            "Monday 9:00 AM should use Monday 8:00 AM weekly schedule"
        )
        
        self.teardown()
    
    def test_date_overrides_weekly(self):
        """Test that specific date schedule overrides weekly schedule."""
        scheduler = self.setup()
        
        # Add weekly schedule: Monday at 8:00 AM
        scheduler.add_schedule(
            chime_filename='weekly_monday.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Monday'],
            name='Monday Morning',
            enabled=True
        )
        
        # Add specific date schedule: 11/10 at 7:00 AM
        scheduler.add_schedule(
            chime_filename='special_date.wav',
            time_str='07:00',
            schedule_type='date',
            month=11,
            day=10,
            name='Special Date',
            enabled=True
        )
        
        # Test on Monday 11/10/2025 at 9:00 AM
        # Both schedules apply, but date should win
        test_time = datetime(2025, 11, 10, 9, 0)
        active_chime = scheduler.get_active_chime(test_time)
        
        self.log_test(
            "Date Overrides Weekly",
            active_chime == 'special_date.wav',
            'special_date.wav',
            active_chime,
            "Date schedule should override weekly schedule on same day"
        )
        
        self.teardown()
    
    def test_holiday_overrides_date(self):
        """Test that holiday schedule overrides specific date schedule."""
        scheduler = self.setup()
        
        # Add specific date schedule: 12/25 at 6:00 AM
        scheduler.add_schedule(
            chime_filename='date_dec25.wav',
            time_str='06:00',
            schedule_type='date',
            month=12,
            day=25,
            name='Dec 25 Date',
            enabled=True
        )
        
        # Add holiday schedule: Christmas at 12:00 AM
        scheduler.add_schedule(
            chime_filename='christmas_holiday.wav',
            time_str='00:00',
            schedule_type='holiday',
            holiday='Christmas Day',
            name='Christmas Holiday',
            enabled=True
        )
        
        # Test on Christmas 2025 at 10:00 AM
        test_time = datetime(2025, 12, 25, 10, 0)
        active_chime = scheduler.get_active_chime(test_time)
        
        self.log_test(
            "Holiday Overrides Date",
            active_chime == 'christmas_holiday.wav',
            'christmas_holiday.wav',
            active_chime,
            "Holiday schedule should override date schedule on Christmas"
        )
        
        self.teardown()
    
    def test_holiday_overrides_weekly(self):
        """Test that holiday schedule overrides weekly schedule."""
        scheduler = self.setup()
        
        # Add weekly schedule: Thursday at 8:00 AM
        scheduler.add_schedule(
            chime_filename='weekly_thursday.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Thursday'],
            name='Thursday Morning',
            enabled=True
        )
        
        # Add holiday schedule: Thanksgiving at 12:00 AM
        # Thanksgiving 2025 is November 27 (4th Thursday)
        scheduler.add_schedule(
            chime_filename='thanksgiving.wav',
            time_str='00:00',
            schedule_type='holiday',
            holiday='Thanksgiving',
            name='Thanksgiving Holiday',
            enabled=True
        )
        
        # Test on Thanksgiving 2025 at 10:00 AM
        test_time = datetime(2025, 11, 27, 10, 0)
        active_chime = scheduler.get_active_chime(test_time)
        
        self.log_test(
            "Holiday Overrides Weekly",
            active_chime == 'thanksgiving.wav',
            'thanksgiving.wav',
            active_chime,
            "Holiday schedule should override weekly schedule on Thanksgiving"
        )
        
        self.teardown()
    
    def test_all_three_types_holiday_wins(self):
        """Test precedence when all three schedule types apply."""
        scheduler = self.setup()
        
        # Add weekly schedule: Thursday at 8:00 AM
        scheduler.add_schedule(
            chime_filename='weekly_thursday.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Thursday'],
            name='Thursday Morning',
            enabled=True
        )
        
        # Add date schedule: 11/27 at 9:00 AM
        scheduler.add_schedule(
            chime_filename='date_1127.wav',
            time_str='09:00',
            schedule_type='date',
            month=11,
            day=27,
            name='Nov 27 Date',
            enabled=True
        )
        
        # Add holiday schedule: Thanksgiving at 12:00 AM
        scheduler.add_schedule(
            chime_filename='thanksgiving.wav',
            time_str='00:00',
            schedule_type='holiday',
            holiday='Thanksgiving',
            name='Thanksgiving Holiday',
            enabled=True
        )
        
        # Test on Thanksgiving 2025 at 10:00 AM
        # All three schedules have passed, holiday should win
        test_time = datetime(2025, 11, 27, 10, 0)
        active_chime = scheduler.get_active_chime(test_time)
        
        self.log_test(
            "All Three Types - Holiday Wins",
            active_chime == 'thanksgiving.wav',
            'thanksgiving.wav',
            active_chime,
            "Holiday schedule should win when all three types apply"
        )
        
        self.teardown()
    
    def test_date_wins_over_weekly_when_no_holiday(self):
        """Test that date beats weekly when no holiday applies."""
        scheduler = self.setup()
        
        # Add weekly schedule: Tuesday at 8:00 AM
        scheduler.add_schedule(
            chime_filename='weekly_tuesday.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Tuesday'],
            name='Tuesday Morning',
            enabled=True
        )
        
        # Add date schedule: 11/11 at 7:00 AM
        scheduler.add_schedule(
            chime_filename='date_1111.wav',
            time_str='07:00',
            schedule_type='date',
            month=11,
            day=11,
            name='Nov 11 Date',
            enabled=True
        )
        
        # Add holiday schedule for different day
        scheduler.add_schedule(
            chime_filename='veterans.wav',
            time_str='00:00',
            schedule_type='holiday',
            holiday='Veterans Day',  # This is also 11/11, so this should actually win!
            name='Veterans Day',
            enabled=True
        )
        
        # Test on Tuesday 11/11/2025 at 9:00 AM
        # Veterans Day IS 11/11, so holiday should win
        test_time = datetime(2025, 11, 11, 9, 0)
        active_chime = scheduler.get_active_chime(test_time)
        
        self.log_test(
            "Veterans Day (Holiday) Wins on 11/11",
            active_chime == 'veterans.wav',
            'veterans.wav',
            active_chime,
            "Holiday (Veterans Day on 11/11) should win over date and weekly"
        )
        
        self.teardown()
    
    def test_multiple_schedules_same_type_latest_wins(self):
        """Test that within same type, latest time wins."""
        scheduler = self.setup()
        
        # Add multiple weekly schedules for Monday
        scheduler.add_schedule(
            chime_filename='monday_morning.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Monday'],
            name='Monday 8 AM',
            enabled=True
        )
        
        scheduler.add_schedule(
            chime_filename='monday_noon.wav',
            time_str='12:00',
            schedule_type='weekly',
            days=['Monday'],
            name='Monday Noon',
            enabled=True
        )
        
        # Test on Monday at 2:00 PM
        test_time = datetime(2025, 11, 10, 14, 0)
        active_chime = scheduler.get_active_chime(test_time)
        
        self.log_test(
            "Multiple Weekly - Latest Wins",
            active_chime == 'monday_noon.wav',
            'monday_noon.wav',
            active_chime,
            "Most recent weekly schedule (noon) should be active at 2 PM"
        )
        
        self.teardown()
    
    def test_disabled_schedule_ignored(self):
        """Test that disabled schedules are ignored."""
        scheduler = self.setup()
        
        # Add disabled holiday schedule
        scheduler.add_schedule(
            chime_filename='christmas_disabled.wav',
            time_str='00:00',
            schedule_type='holiday',
            holiday='Christmas Day',
            name='Christmas Disabled',
            enabled=False  # Disabled!
        )
        
        # Add enabled weekly schedule for Thursday
        scheduler.add_schedule(
            chime_filename='thursday_enabled.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Thursday'],
            name='Thursday Enabled',
            enabled=True
        )
        
        # Test on Christmas 2025 (Thursday) at 10:00 AM
        # Holiday is disabled, so weekly should be used
        test_time = datetime(2025, 12, 25, 10, 0)
        active_chime = scheduler.get_active_chime(test_time)
        
        self.log_test(
            "Disabled Schedule Ignored",
            active_chime == 'thursday_enabled.wav',
            'thursday_enabled.wav',
            active_chime,
            "Disabled holiday schedule should be ignored, weekly should be used"
        )
        
        self.teardown()
    
    def test_movable_holiday_calculation(self):
        """Test that movable holidays are calculated correctly."""
        scheduler = self.setup()
        
        # Add Thanksgiving holiday schedule
        scheduler.add_schedule(
            chime_filename='thanksgiving.wav',
            time_str='00:00',
            schedule_type='holiday',
            holiday='Thanksgiving',
            name='Thanksgiving',
            enabled=True
        )
        
        # Thanksgiving 2025 is November 27 (4th Thursday of November)
        test_time = datetime(2025, 11, 27, 10, 0)
        active_chime = scheduler.get_active_chime(test_time)
        
        self.log_test(
            "Movable Holiday (Thanksgiving)",
            active_chime == 'thanksgiving.wav',
            'thanksgiving.wav',
            active_chime,
            "Thanksgiving should be correctly calculated as Nov 27, 2025"
        )
        
        # Test that it doesn't trigger on wrong Thursday
        test_time_wrong = datetime(2025, 11, 20, 10, 0)  # 3rd Thursday
        active_chime_wrong = scheduler.get_active_chime(test_time_wrong)
        
        self.log_test(
            "Movable Holiday (Not on Wrong Thursday)",
            active_chime_wrong is None,
            None,
            active_chime_wrong,
            "Thanksgiving should NOT trigger on 3rd Thursday (Nov 20)"
        )
        
        self.teardown()
    
    def run_all_tests(self):
        """Run all tests and print summary."""
        print("=" * 70)
        print("CHIME SCHEDULER PRECEDENCE TESTS")
        print("=" * 70)
        print()
        
        self.test_weekly_schedule_only()
        self.test_date_overrides_weekly()
        self.test_holiday_overrides_date()
        self.test_holiday_overrides_weekly()
        self.test_all_three_types_holiday_wins()
        self.test_date_wins_over_weekly_when_no_holiday()
        self.test_multiple_schedules_same_type_latest_wins()
        self.test_disabled_schedule_ignored()
        self.test_movable_holiday_calculation()
        
        print()
        print("=" * 70)
        print("TEST SUMMARY")
        print("=" * 70)
        
        passed = sum(1 for r in self.test_results if r['passed'])
        total = len(self.test_results)
        
        print(f"Passed: {passed}/{total}")
        print(f"Failed: {total - passed}/{total}")
        
        if passed == total:
            print()
            print("🎉 ALL TESTS PASSED! 🎉")
            return 0
        else:
            print()
            print("⚠️  SOME TESTS FAILED")
            print()
            print("Failed tests:")
            for result in self.test_results:
                if not result['passed']:
                    print(f"  - {result['name']}")
            return 1


if __name__ == '__main__':
    tester = TestSchedulePrecedence()
    exit_code = tester.run_all_tests()
    sys.exit(exit_code)
