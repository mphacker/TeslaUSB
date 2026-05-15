"""Tests for the cloud-sync folder rework (RecentClips → ArchivedClips).

The Cloud Sync settings page used to list ``RecentClips`` as a sync
target. That was a foot-gun:

* Tesla rotates ``RecentClips`` on a 1-hour ring; uploading from it is
  racey and wasteful (the archive subsystem copies *survivors* to
  ``ArchivedClips`` on SD before they age out).
* ``_discover_events`` walks per-event subdirectories — ``RecentClips``
  is flat files, so the checkbox silently did nothing.
* ``ArchivedClips`` was already being scanned, but the UI offered no
  way to turn it on or off, and the configured ``priority_order`` was
  read at boot and then thrown away (never used downstream).

This rework:

* swaps ``RecentClips`` → ``ArchivedClips`` everywhere in the UI / config
  defaults / live normalization
* honors the checkbox for ``ArchivedClips`` (no more always-on
  silent-upload)
* wires ``priority_order`` into the discovery sort so the configured
  folder ordering becomes the primary sort axis

These tests pin the public contract.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_WEB = Path(__file__).resolve().parent.parent / "scripts" / "web"
if str(SCRIPTS_WEB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_WEB))

from services import cloud_archive_service as cas  # noqa: E402
import config as web_config  # noqa: E402


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


class TestNormalizeFolderList:
    """Service-internal normalizer (used by every YAML read path)."""

    def test_recentclips_rewritten_to_archivedclips(self):
        assert cas._normalize_folder_list(["RecentClips"]) == ["ArchivedClips"]

    def test_recentclips_in_middle_rewritten(self):
        assert cas._normalize_folder_list(
            ["SentryClips", "RecentClips", "SavedClips"]
        ) == ["SentryClips", "ArchivedClips", "SavedClips"]

    def test_unknown_folder_dropped(self):
        assert cas._normalize_folder_list(["SentryClips", "WeirdFolder"]) == [
            "SentryClips"
        ]

    def test_dedup_preserves_order(self):
        assert cas._normalize_folder_list(
            ["SavedClips", "SentryClips", "SavedClips"]
        ) == ["SavedClips", "SentryClips"]

    def test_empty_list_returns_empty(self):
        """Normalizer is pure — fallback to default is the reader's job."""
        assert cas._normalize_folder_list([]) == []

    def test_none_returns_empty(self):
        """None / non-list inputs collapse to empty so the reader can
        notice "user has not configured a list" and fall back."""
        assert cas._normalize_folder_list(None) == []

    def test_archivedclips_passthrough(self):
        assert cas._normalize_folder_list(
            ["SentryClips", "SavedClips", "ArchivedClips"]
        ) == ["SentryClips", "SavedClips", "ArchivedClips"]


class TestConfigNormalizeCloudFolderList:
    """Config wrapper normalizer (used at boot + by blueprint)."""

    def test_recentclips_rewritten(self):
        assert web_config._normalize_cloud_folder_list(
            ["RecentClips"], ["SentryClips", "SavedClips", "ArchivedClips"]
        ) == ["ArchivedClips"]

    def test_legacy_full_list_rewritten(self):
        result = web_config._normalize_cloud_folder_list(
            ["SentryClips", "SavedClips", "RecentClips"],
            ["SentryClips", "SavedClips", "ArchivedClips"],
        )
        assert "RecentClips" not in result
        assert "ArchivedClips" in result
        assert result.index("SentryClips") < result.index("SavedClips")

    def test_default_when_empty(self):
        result = web_config._normalize_cloud_folder_list(
            [], ["SentryClips", "SavedClips", "ArchivedClips"]
        )
        assert result == ["SentryClips", "SavedClips", "ArchivedClips"]

    def test_default_includes_archivedclips(self):
        """Module-level default must NOT contain RecentClips."""
        assert "RecentClips" not in web_config.CLOUD_ARCHIVE_SYNC_FOLDERS
        assert "ArchivedClips" in web_config.CLOUD_ARCHIVE_SYNC_FOLDERS
        assert "RecentClips" not in web_config.CLOUD_ARCHIVE_PRIORITY_ORDER
        assert "ArchivedClips" in web_config.CLOUD_ARCHIVE_PRIORITY_ORDER


# ---------------------------------------------------------------------------
# Folder priority sort
# ---------------------------------------------------------------------------


class TestFolderPriorityIndex:
    def test_in_order(self):
        order = ["SentryClips", "SavedClips", "ArchivedClips"]
        assert cas._folder_priority_index("SentryClips", order) == 0
        assert cas._folder_priority_index("SavedClips", order) == 1
        assert cas._folder_priority_index("ArchivedClips", order) == 2

    def test_unknown_sorts_after_known(self):
        order = ["SentryClips", "SavedClips"]
        # Unknown gets len(order) → sorts AFTER configured folders.
        assert cas._folder_priority_index("ArchivedClips", order) == 2
        assert cas._folder_priority_index("Whatever", order) == 2

    def test_empty_order(self):
        assert cas._folder_priority_index("SentryClips", []) == 0


class TestFolderOfEventRel:
    def test_sentryclips_prefix(self):
        assert cas._folder_of_event_rel("SentryClips/2026-01-01_12-00-00") == "SentryClips"

    def test_savedclips_prefix(self):
        assert cas._folder_of_event_rel("SavedClips/foo/bar") == "SavedClips"

    def test_archivedclips_prefix(self):
        assert cas._folder_of_event_rel("ArchivedClips/clip.mp4") == "ArchivedClips"

    def test_no_separator_returns_empty(self):
        """Defensive: a malformed path with no leading folder component
        returns ``""`` so the priority sort puts it at the end."""
        assert cas._folder_of_event_rel("SentryClips") == ""

    def test_empty_string_returns_empty(self):
        assert cas._folder_of_event_rel("") == ""

    def test_leading_slash_returns_empty_folder(self):
        # A path that starts with "/" has an empty leading component.
        assert cas._folder_of_event_rel("/SentryClips/foo") == ""

    def test_priority_multiplier_dominates_content_score(self):
        """Folder axis must outrank the per-event content score."""
        # Max content score is ~299 (200 event-folder bonus + 99 age cap).
        # _FOLDER_PRIORITY_MULTIPLIER must be strictly greater.
        assert cas._FOLDER_PRIORITY_MULTIPLIER > 299


# ---------------------------------------------------------------------------
# YAML readers honoring live config
# ---------------------------------------------------------------------------


class TestLiveYamlReaders:
    def test_sync_folders_reader_returns_list(self):
        result = cas._read_sync_folders_setting()
        assert isinstance(result, list)
        # Whatever it returns must be normalized.
        assert "RecentClips" not in result
        for entry in result:
            assert entry in cas._VALID_SYNC_FOLDERS

    def test_priority_order_reader_returns_list(self):
        result = cas._read_priority_order_setting()
        assert isinstance(result, list)
        assert "RecentClips" not in result


# ---------------------------------------------------------------------------
# Reconcile contract — must scan EVERY historical folder regardless of
# current user selection (otherwise unchecking SentryClips would trigger
# re-upload when it's checked again).
# ---------------------------------------------------------------------------


class TestReconcileFolderContract:
    def test_event_folder_names_is_tesla_canonical(self):
        """Tesla firmware fixes the event-folder names — they cannot
        be user-configured because Tesla writes to them directly."""
        assert cas._EVENT_FOLDER_NAMES == ("SentryClips", "SavedClips")

    def test_known_cloud_roots_includes_all_historical(self):
        """Reconciliation scans every folder Tesla / TeslaUSB has ever
        written to so unchecking a folder can't lose dedup info."""
        roots = set(cas._KNOWN_CLOUD_ROOTS)
        for required in ("SentryClips", "SavedClips", "RecentClips", "ArchivedClips"):
            assert required in roots, (
                f"{required!r} missing from _KNOWN_CLOUD_ROOTS — re-checking "
                f"the folder after un-checking would re-upload everything"
            )
