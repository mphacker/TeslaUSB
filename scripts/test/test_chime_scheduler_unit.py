#!/usr/bin/env python3
"""
Comprehensive unit tests for chime scheduler.

Tests the core logic without requiring actual file system operations or chime changes.
Uses temporary schedule files and mocked time to verify:
- Schedule evaluation logic
- Multi-schedule precedence
- Execution tracking
- Cleanup of expired date schedules
"""

import sys
import os
import tempfile
import json
from pathlib import Path
from datetime import datetime, time as datetime_time

# Add scripts/web to path
SCRIPT_DIR = Path(__file__).parent.resolve()
WEB_DIR = SCRIPT_DIR.parent / 'web'
sys.path.insert(0, str(WEB_DIR))

from services.chime_scheduler_service import ChimeScheduler, cleanup_expired_date_schedules


class TestResults:
    """Track test results."""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.tests = []
    
    def add(self, name, passed, expected, actual, details=""):
        self.tests.append({
            'name': name,
            'passed': passed,
            'expected': expected,
            'actual': actual,
            'details': details
        })
        if passed:
            self.passed += 1
        else:
            self.failed += 1
    
    def print_summary(self):
        print("\n" + "=" * 80)
        print("TEST SUMMARY")
        print("=" * 80)
        for test in self.tests:
            status = "✅ PASS" if test['passed'] else "❌ FAIL"
            print(f"{status} {test['name']}")
            if not test['passed']:
                print(f"   Expected: {test['expected']}")
                print(f"   Actual:   {test['actual']}")
                if test['details']:
                    print(f"   Details:  {test['details']}")
        print("=" * 80)
        print(f"Total: {self.passed + self.failed} tests")
        print(f"Passed: {self.passed}")
        print(f"Failed: {self.failed}")
        print("=" * 80)
        return self.failed == 0


def create_test_scheduler():
    """Create a temporary scheduler for testing."""
    fd, temp_file = tempfile.mkstemp(suffix='.json', prefix='test_schedules_')
    os.write(fd, b'[]')
    os.close(fd)
    return ChimeScheduler(temp_file), temp_file


def test_single_schedule_future(results):
    """Test: Schedule in future should not execute."""
    print("\n--- Test: Single schedule in future ---")
    scheduler, temp_file = create_test_scheduler()
    
    try:
        # Create schedule for 10:00 AM today
        scheduler.add_schedule(
            chime_filename='test_chime.wav',
            time_str='10:00',
            schedule_type='weekly',
            days=['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
            name='Test Future',
            enabled=True
        )
        
        # Check at 9:00 AM (before scheduled time)
        check_time = datetime(2025, 11, 25, 9, 0, 0)
        should_run, chime, reason = scheduler.should_execute_schedule(1, check_time)
        
        results.add(
            "Future schedule should not execute",
            should_run == False,
            False,
            should_run,
            reason
        )
        print(f"  Result: should_run={should_run}, reason={reason}")
    
    finally:
        os.remove(temp_file)


def test_single_schedule_past(results):
    """Test: Schedule in past should execute (if not already run)."""
    print("\n--- Test: Single schedule in past ---")
    scheduler, temp_file = create_test_scheduler()
    
    try:
        # Create schedule for 8:00 AM today
        scheduler.add_schedule(
            chime_filename='test_chime.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
            name='Test Past',
            enabled=True
        )
        
        # Check at 9:00 AM (after scheduled time)
        check_time = datetime(2025, 11, 25, 9, 0, 0)
        should_run, chime, reason = scheduler.should_execute_schedule(1, check_time)
        
        results.add(
            "Past schedule should execute (first run)",
            should_run == True,
            True,
            should_run,
            reason
        )
        print(f"  Result: should_run={should_run}, reason={reason}")
    
    finally:
        os.remove(temp_file)


def test_schedule_already_run_today(results):
    """Test: Schedule should not run twice in same day."""
    print("\n--- Test: Schedule already run today ---")
    scheduler, temp_file = create_test_scheduler()
    
    try:
        # Create schedule for 8:00 AM
        scheduler.add_schedule(
            chime_filename='test_chime.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
            name='Test Already Run',
            enabled=True
        )
        
        # Record execution at 8:01 AM
        execution_time = datetime(2025, 11, 25, 8, 1, 0)
        scheduler.record_execution(1, execution_time)
        
        # Check at 9:00 AM (should not run again)
        check_time = datetime(2025, 11, 25, 9, 0, 0)
        should_run, chime, reason = scheduler.should_execute_schedule(1, check_time)
        
        results.add(
            "Schedule should not run twice in same day",
            should_run == False,
            False,
            should_run,
            reason
        )
        print(f"  Result: should_run={should_run}, reason={reason}")
    
    finally:
        os.remove(temp_file)


def test_multiple_schedules_catch_up(results):
    """Test: Multiple missed schedules should execute most recent only."""
    print("\n--- Test: Multiple schedules catch-up ---")
    scheduler, temp_file = create_test_scheduler()
    
    try:
        # Create three schedules: 8:00 AM, 10:00 AM, 3:00 PM
        scheduler.add_schedule(
            chime_filename='morning.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Monday'],
            name='Morning',
            enabled=True
        )
        scheduler.add_schedule(
            chime_filename='midday.wav',
            time_str='10:00',
            schedule_type='weekly',
            days=['Monday'],
            name='Midday',
            enabled=True
        )
        scheduler.add_schedule(
            chime_filename='afternoon.wav',
            time_str='15:00',
            schedule_type='weekly',
            days=['Monday'],
            name='Afternoon',
            enabled=True
        )
        
        # Check at 4:00 PM (all three have passed)
        check_time = datetime(2025, 11, 24, 16, 0, 0)  # Monday Nov 24
        
        # Find which schedules should execute
        eligible = []
        for schedule in scheduler.schedules:
            should_run, chime, reason = scheduler.should_execute_schedule(schedule['id'], check_time)
            if should_run:
                eligible.append((schedule['id'], schedule['time'], chime))
        
        results.add(
            "All three schedules should be eligible",
            len(eligible) == 3,
            3,
            len(eligible),
            f"Eligible: {eligible}"
        )
        
        # Most recent should be afternoon (15:00)
        if eligible:
            eligible.sort(key=lambda x: x[1], reverse=True)
            most_recent = eligible[0]
            results.add(
                "Most recent schedule should be afternoon (15:00)",
                most_recent[1] == '15:00',
                '15:00',
                most_recent[1],
                f"Chime: {most_recent[2]}"
            )
            print(f"  Eligible schedules: {len(eligible)}")
            print(f"  Most recent: {most_recent[1]} ({most_recent[2]})")
    
    finally:
        os.remove(temp_file)


def test_schedule_precedence(results):
    """Test: Holiday > Date > Weekly precedence at same time."""
    print("\n--- Test: Schedule type precedence ---")
    scheduler, temp_file = create_test_scheduler()
    
    try:
        # Create three schedules for same time (8:00 AM) on Christmas (12/25)
        # Weekly: Every Thursday
        scheduler.add_schedule(
            chime_filename='weekly.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Thursday'],
            name='Weekly Thursday',
            enabled=True
        )
        
        # Date: December 25
        scheduler.add_schedule(
            chime_filename='date.wav',
            time_str='08:00',
            schedule_type='date',
            month=12,
            day=25,
            name='Dec 25 Date',
            enabled=True
        )
        
        # Holiday: Christmas
        scheduler.add_schedule(
            chime_filename='holiday.wav',
            time_str='08:00',
            schedule_type='holiday',
            holiday='Christmas Day',
            name='Christmas Holiday',
            enabled=True
        )
        
        # Check on Christmas 2025 (Thursday, Dec 25) at 9:00 AM
        check_time = datetime(2025, 12, 25, 9, 0, 0)
        
        # All should be eligible
        eligible = []
        for schedule in scheduler.schedules:
            should_run, chime, reason = scheduler.should_execute_schedule(schedule['id'], check_time)
            if should_run:
                eligible.append((schedule['schedule_type'], chime))
        
        results.add(
            "All three schedule types should be eligible",
            len(eligible) == 3,
            3,
            len(eligible),
            f"Eligible: {eligible}"
        )
        
        # Holiday should win by precedence
        type_priority = {'holiday': 3, 'date': 2, 'weekly': 1}
        if eligible:
            eligible.sort(key=lambda x: type_priority.get(x[0], 0), reverse=True)
            winner = eligible[0]
            results.add(
                "Holiday schedule should have highest precedence",
                winner[0] == 'holiday',
                'holiday',
                winner[0],
                f"Chime: {winner[1]}"
            )
            print(f"  Winner: {winner[0]} ({winner[1]})")
    
    finally:
        os.remove(temp_file)


def test_cleanup_expired_date_schedules(results):
    """Test: Expired date schedules should be deleted after execution."""
    print("\n--- Test: Cleanup expired date schedules ---")
    scheduler, temp_file = create_test_scheduler()
    
    try:
        # Create date schedule for yesterday
        scheduler.add_schedule(
            chime_filename='yesterday.wav',
            time_str='08:00',
            schedule_type='date',
            month=11,
            day=24,
            name='Yesterday',
            enabled=True
        )
        
        # Create date schedule for tomorrow
        scheduler.add_schedule(
            chime_filename='tomorrow.wav',
            time_str='08:00',
            schedule_type='date',
            month=11,
            day=26,
            name='Tomorrow',
            enabled=True
        )
        
        # Mark yesterday's schedule as executed
        scheduler.record_execution(1, datetime(2025, 11, 24, 8, 1, 0))
        
        # Run cleanup at current time (Nov 25)
        check_time = datetime(2025, 11, 25, 9, 0, 0)
        deleted_count = cleanup_expired_date_schedules(scheduler, check_time)
        
        results.add(
            "Should delete 1 expired date schedule",
            deleted_count == 1,
            1,
            deleted_count,
            "Yesterday's executed schedule should be deleted"
        )
        
        # Verify only tomorrow's schedule remains
        remaining = scheduler.list_schedules()
        results.add(
            "Should have 1 schedule remaining (tomorrow)",
            len(remaining) == 1,
            1,
            len(remaining),
            f"Remaining: {[s['name'] for s in remaining]}"
        )
        
        if remaining:
            results.add(
                "Remaining schedule should be tomorrow's",
                remaining[0]['name'] == 'Tomorrow',
                'Tomorrow',
                remaining[0]['name']
            )
        
        print(f"  Deleted: {deleted_count}")
        print(f"  Remaining: {len(remaining)}")
    
    finally:
        os.remove(temp_file)


def test_cleanup_keeps_unexecuted_past_dates(results):
    """Test: Past date schedules that haven't run should NOT be deleted."""
    print("\n--- Test: Keep unexecuted past date schedules ---")
    scheduler, temp_file = create_test_scheduler()
    
    try:
        # Create date schedule for yesterday (NOT executed)
        scheduler.add_schedule(
            chime_filename='yesterday_missed.wav',
            time_str='08:00',
            schedule_type='date',
            month=11,
            day=24,
            name='Yesterday Missed',
            enabled=True
        )
        
        # Run cleanup (should NOT delete unexecuted schedule)
        check_time = datetime(2025, 11, 25, 9, 0, 0)
        deleted_count = cleanup_expired_date_schedules(scheduler, check_time)
        
        results.add(
            "Should NOT delete unexecuted past schedules",
            deleted_count == 0,
            0,
            deleted_count,
            "Allows catch-up execution on next tick"
        )
        
        remaining = scheduler.list_schedules()
        results.add(
            "Unexecuted past schedule should remain",
            len(remaining) == 1,
            1,
            len(remaining)
        )
        
        print(f"  Deleted: {deleted_count}")
        print(f"  Remaining: {len(remaining)}")
    
    finally:
        os.remove(temp_file)


def test_cleanup_ignores_weekly_schedules(results):
    """Test: Weekly schedules should never be deleted by cleanup."""
    print("\n--- Test: Weekly schedules not affected by cleanup ---")
    scheduler, temp_file = create_test_scheduler()
    
    try:
        # Create weekly schedule
        scheduler.add_schedule(
            chime_filename='weekly.wav',
            time_str='08:00',
            schedule_type='weekly',
            days=['Monday'],
            name='Weekly',
            enabled=True
        )
        
        # Mark as executed yesterday
        scheduler.record_execution(1, datetime(2025, 11, 24, 8, 1, 0))
        
        # Run cleanup
        check_time = datetime(2025, 11, 25, 9, 0, 0)
        deleted_count = cleanup_expired_date_schedules(scheduler, check_time)
        
        results.add(
            "Weekly schedules should not be deleted",
            deleted_count == 0,
            0,
            deleted_count
        )
        
        remaining = scheduler.list_schedules()
        results.add(
            "Weekly schedule should remain",
            len(remaining) == 1,
            1,
            len(remaining)
        )
        
        print(f"  Deleted: {deleted_count}")
        print(f"  Remaining: {len(remaining)}")
    
    finally:
        os.remove(temp_file)


def main():
    """Run all unit tests."""
    print("=" * 80)
    print("CHIME SCHEDULER UNIT TESTS")
    print("=" * 80)
    
    results = TestResults()
    
    # Run all tests
    test_single_schedule_future(results)
    test_single_schedule_past(results)
    test_schedule_already_run_today(results)
    test_multiple_schedules_catch_up(results)
    test_schedule_precedence(results)
    test_cleanup_expired_date_schedules(results)
    test_cleanup_keeps_unexecuted_past_dates(results)
    test_cleanup_ignores_weekly_schedules(results)
    
    # Print summary
    all_passed = results.print_summary()
    
    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())
