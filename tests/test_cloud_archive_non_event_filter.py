"""Tests for Phase 2.3 — sync_non_event_videos filter actually filters.

When ``cloud_archive.sync_non_event_videos`` is False (the default), the
cloud archive sync picker must DROP non-event/non-geo clips from the queue
entirely — not merely demote them to lower priority. The pre-fix behaviour
silently uploaded those clips anyway, eating user bandwidth and slowing
down the upload of the event clips users actually care about.

These tests pin both directions of the toggle, AND drive the
config-change path through ``update_config_yaml`` (the real Settings
save handler) — NOT by monkeypatching the in-memory ``config`` module.
The first iteration of this PR was caught in review using exactly this
test technique: monkeypatching ``config.CLOUD_ARCHIVE_SYNC_NON_EVENT``
masked the fact that the production save handler only writes YAML, so
the picker had to re-read YAML directly to honour the
""no-restart-needed"" contract. These tests now exercise that path
end-to-end.
"""

from __future__ import annotations

import os
from typing import List, Tuple

import pytest

import config
from helpers.config_updater import update_config_yaml
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
def isolated_yaml(tmp_path, monkeypatch):
    """Point ``CONFIG_YAML`` at a writable copy so tests can drive the
    real ``update_config_yaml`` path without poisoning the repo's
    config.yaml. Returns the path so individual tests can pre-seed the
    flag if desired.
    """
    yaml_path = tmp_path / "config.yaml"
    # Minimal config matching the keys the picker reads.
    yaml_path.write_text(
        "cloud_archive:\n  sync_non_event_videos: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML", str(yaml_path))
    # Also patch the helper's import-time copy.
    import helpers.config_updater as cu
    monkeypatch.setattr(cu, "CONFIG_YAML", str(yaml_path))
    return str(yaml_path)


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

    def test_filter_off_drops_non_event_clips(
            self, teslacam_root, isolated_yaml):
        """Default (flag absent → False): only the event clip remains."""
        # isolated_yaml seeds sync_non_event_videos: false
        result = svc._discover_events(teslacam_root, conn=None)

        assert len(result) == 1, (
            f"Expected only the event clip, got {[r[1] for r in result]}"
        )
        assert result[0][1] == "SentryClips/2026-05-12_10-00-00"

    def test_filter_on_keeps_non_event_clips(
            self, teslacam_root, isolated_yaml):
        """When the flag is True, both clips remain (legacy behaviour)."""
        update_config_yaml({'cloud_archive.sync_non_event_videos': True})

        result = svc._discover_events(teslacam_root, conn=None)

        rel_paths = sorted(r[1] for r in result)
        assert rel_paths == [
            "SentryClips/2026-05-12_10-00-00",
            "SentryClips/2026-05-12_11-00-00",
        ]

    def test_filter_off_with_only_non_event_clips_returns_empty(
            self, tmp_path, isolated_yaml, monkeypatch):
        """If every candidate is non-event/non-geo, the queue is empty."""
        teslacam = tmp_path / "TeslaCam2"
        sentry = teslacam / "SentryClips"
        sentry.mkdir(parents=True)
        _make_event_dir(str(sentry), "2026-05-12_09-00-00",
                        with_event_json=False)
        _make_event_dir(str(sentry), "2026-05-12_10-00-00",
                        with_event_json=False)
        monkeypatch.setattr(config, "MAPPING_ENABLED", False, raising=False)

        result = svc._discover_events(str(teslacam), conn=None)

        assert result == []

    def test_filter_change_takes_effect_without_restart(
            self, teslacam_root, isolated_yaml):
        """Toggling the flag through the real Settings save path picks up
        the new value on the next call.

        This is THE contract this PR exists to ship: the prior version
        of the picker re-imported a module attribute from ``config`` —
        which is set once at import time and is never refreshed by
        ``update_config_yaml``. Result: a Settings change had no effect
        until ``gadget_web.service`` was restarted, and a user toggling
        ``false → true`` would silently keep filtering forever.

        If this test starts failing, the picker has regressed to
        import-time caching and we'll silently re-introduce the same
        bug the original review caught.
        """
        # Seed: flag is off (default), routine clip filtered out.
        first = svc._discover_events(teslacam_root, conn=None)
        assert len(first) == 1, "expected filter-on by default"

        # Drive the change through the EXACT path the Settings handler
        # uses — no in-memory module mutation, no service restart.
        update_config_yaml({'cloud_archive.sync_non_event_videos': True})

        # Next call should now include the routine clip.
        second = svc._discover_events(teslacam_root, conn=None)
        assert len(second) == 2, (
            "Picker did not pick up the YAML change without restart — "
            "regression to import-time caching of "
            "CLOUD_ARCHIVE_SYNC_NON_EVENT."
        )

    def test_filter_logs_drop_count(
            self, teslacam_root, isolated_yaml, caplog):
        """The filter must log how many clips it dropped (for diagnosis)."""
        import logging

        with caplog.at_level(logging.INFO, logger=svc.logger.name):
            svc._discover_events(teslacam_root, conn=None)

        assert any(
            "filtered 1 non-event" in rec.message
            for rec in caplog.records
        ), f"Expected drop-count log line; got {[r.message for r in caplog.records]}"

    def test_picker_falls_back_safely_when_yaml_unreadable(
            self, teslacam_root, isolated_yaml, monkeypatch):
        """If YAML read fails, the picker falls back to import-time value
        rather than crashing the worker.

        Pins the fail-safe contract: the worker thread must keep running
        even if config.yaml is missing/corrupt/locked at the moment of a
        sync iteration.
        """
        monkeypatch.setattr(config, "CONFIG_YAML", "/nonexistent/path/config.yaml")
        # Import-time fallback is whatever was loaded at module import
        # — we don't assert a specific count here, only that the call
        # doesn't raise.
        result = svc._discover_events(teslacam_root, conn=None)
        assert isinstance(result, list)
