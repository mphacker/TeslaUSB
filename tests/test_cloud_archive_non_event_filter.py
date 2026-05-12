"""Tests for Phase 2.3 — sync_non_event_videos filter actually filters.

When ``cloud_archive.sync_non_event_videos`` is False (the default), the
cloud archive sync picker must DROP non-event/non-geo clips from the queue
entirely — not merely demote them to lower priority. The pre-fix behaviour
silently uploaded those clips anyway, eating user bandwidth and slowing
down the upload of the event clips users actually care about.

These tests pin both directions of the toggle:

* ``sync_non_event_videos=False`` → score >= 200 entries dropped, score < 200
  entries preserved.
* ``sync_non_event_videos=True`` → all entries preserved (current legacy
  behaviour kept intact for users who explicitly opt in).
* The flag is read from ``config`` on every call so Settings changes take
  effect on the next sync iteration without a service restart.
"""

from __future__ import annotations

import os
from typing import List, Tuple

import pytest

import config
from services import cloud_archive_service as svc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event_dir(parent: str, name: str, with_event_json: bool,
                    with_video: bool = True) -> str:
    """Create a fake event directory matching Tesla's on-disk layout.

    ``with_event_json=True`` will produce a folder that scores 0 (Tesla
    event trigger). Otherwise the folder scores 200+ unless geodata.db
    contains a matching waypoint (which the tests deliberately avoid by
    disabling MAPPING_ENABLED).
    """
    event_dir = os.path.join(parent, name)
    os.makedirs(event_dir, exist_ok=True)
    if with_event_json:
        # Real Tesla event.json — minimal valid JSON with a reason.
        with open(os.path.join(event_dir, "event.json"), "w") as f:
            f.write('{"reason":"sentry_aware_object_detection"}')
    if with_video:
        # Tesla writes one MP4 per camera per minute; one is enough for the
        # discover loop's "has_video" check.
        with open(os.path.join(event_dir, "front.mp4"), "wb") as f:
            f.write(b"\x00" * 1024)
    return event_dir


@pytest.fixture
def teslacam_root(tmp_path, monkeypatch):
    """Build a fake TeslaCam directory with one event clip and one routine
    (non-event, non-geo) clip in SentryClips. Disables MAPPING_ENABLED so
    the geolocation tier is unreachable, ensuring routine clips score 200+.
    """
    teslacam = tmp_path / "TeslaCam"
    sentry = teslacam / "SentryClips"
    sentry.mkdir(parents=True)

    _make_event_dir(str(sentry), "2026-05-12_10-00-00",
                    with_event_json=True)            # score = 0
    _make_event_dir(str(sentry), "2026-05-12_11-00-00",
                    with_event_json=False)           # score >= 200

    # Disable mapping so _score_event_priority can't fall into the
    # geolocation tier (score 100) and accidentally save the routine clip.
    monkeypatch.setattr(config, "MAPPING_ENABLED", False, raising=False)

    return str(teslacam)


# ---------------------------------------------------------------------------
# Tests — Phase 2.3
# ---------------------------------------------------------------------------

class TestSyncNonEventVideosFilter:
    """``sync_non_event_videos`` must actually drop non-event clips."""

    def test_filter_off_drops_non_event_clips(self, teslacam_root, monkeypatch):
        """When the flag is False, only the event clip should remain."""
        monkeypatch.setattr(config, "CLOUD_ARCHIVE_SYNC_NON_EVENT", False)
        # Module-level binding is read once at import; the picker re-imports
        # from config on every call, so patching config alone is sufficient
        # — but we also patch the module binding to defend against any
        # legacy fallback path.
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_SYNC_NON_EVENT", False)

        result = svc._discover_events(teslacam_root, conn=None)

        assert len(result) == 1, (
            f"Expected only the event clip, got {[r[1] for r in result]}"
        )
        assert result[0][1] == "SentryClips/2026-05-12_10-00-00"

    def test_filter_on_keeps_non_event_clips(self, teslacam_root, monkeypatch):
        """When the flag is True, both clips remain (legacy behaviour)."""
        monkeypatch.setattr(config, "CLOUD_ARCHIVE_SYNC_NON_EVENT", True)
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_SYNC_NON_EVENT", True)

        result = svc._discover_events(teslacam_root, conn=None)

        rel_paths = sorted(r[1] for r in result)
        assert rel_paths == [
            "SentryClips/2026-05-12_10-00-00",
            "SentryClips/2026-05-12_11-00-00",
        ]

    def test_filter_off_with_only_non_event_clips_returns_empty(
            self, tmp_path, monkeypatch):
        """If every candidate is non-event/non-geo, the queue is empty."""
        teslacam = tmp_path / "TeslaCam"
        sentry = teslacam / "SentryClips"
        sentry.mkdir(parents=True)
        _make_event_dir(str(sentry), "2026-05-12_09-00-00",
                        with_event_json=False)
        _make_event_dir(str(sentry), "2026-05-12_10-00-00",
                        with_event_json=False)
        monkeypatch.setattr(config, "MAPPING_ENABLED", False, raising=False)
        monkeypatch.setattr(config, "CLOUD_ARCHIVE_SYNC_NON_EVENT", False)
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_SYNC_NON_EVENT", False)

        result = svc._discover_events(str(teslacam), conn=None)

        assert result == []

    def test_filter_change_takes_effect_without_restart(
            self, teslacam_root, monkeypatch):
        """Toggling the flag between calls picks up the new value.

        Pins the contract: the picker re-reads ``config`` per call, so a
        Settings change is honoured on the next sync iteration.
        """
        monkeypatch.setattr(config, "CLOUD_ARCHIVE_SYNC_NON_EVENT", False)
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_SYNC_NON_EVENT", False)
        first = svc._discover_events(teslacam_root, conn=None)
        assert len(first) == 1

        # Flip the flag — no module reload, no service restart.
        monkeypatch.setattr(config, "CLOUD_ARCHIVE_SYNC_NON_EVENT", True)
        # Note: NOT updating svc.CLOUD_ARCHIVE_SYNC_NON_EVENT here — the
        # picker MUST be reading the live ``config`` module, not its own
        # import-time binding. If this test starts failing, the picker has
        # silently regressed to import-time caching.
        second = svc._discover_events(teslacam_root, conn=None)
        assert len(second) == 2

    def test_filter_logs_drop_count(self, teslacam_root, monkeypatch, caplog):
        """The filter must log how many clips it dropped (for diagnosis)."""
        import logging
        monkeypatch.setattr(config, "CLOUD_ARCHIVE_SYNC_NON_EVENT", False)
        monkeypatch.setattr(svc, "CLOUD_ARCHIVE_SYNC_NON_EVENT", False)

        with caplog.at_level(logging.INFO, logger=svc.logger.name):
            svc._discover_events(teslacam_root, conn=None)

        assert any(
            "filtered 1 non-event" in rec.message
            for rec in caplog.records
        ), f"Expected drop-count log line; got {[r.message for r in caplog.records]}"
