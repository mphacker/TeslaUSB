"""Tests for ``scripts/web/config.py`` — focused on the duplicate-key
detection added in issue #220 to catch silent ``config.yaml``
last-wins overrides at boot.

The historical bug: ``yaml.safe_load`` silently keeps the LAST value
when a mapping key appears twice. On the Pi this caused
``shadow_pipeline_queue`` and ``use_pipeline_reader`` to be flipped to
their opposites for weeks before the contradiction was spotted by
hand. The fix is a SafeLoader subclass that raises with line numbers
on any duplicate, so boot fails loudly instead of silently corrupting
behavior.
"""

from __future__ import annotations

import pytest
import yaml

from config import (  # type: ignore[import-not-found]
    _DuplicateKeyDetectingLoader,
    _DuplicateKeyError,
)


class TestDuplicateKeyDetection:
    """The custom loader must accept all valid YAML and refuse any
    document containing a duplicate mapping key."""

    def test_unique_keys_load_normally(self):
        # Sanity — every valid PyYAML construct must still work.
        doc = """
        installation:
          target_user: pi
          mount_dir: /mnt/gadget
        disk_images:
          cam_name: usb_cam.img
          lightshow_name: usb_lightshow.img
        nested:
          deeper:
            key: value
        """
        result = yaml.load(doc, Loader=_DuplicateKeyDetectingLoader)
        assert result['installation']['target_user'] == 'pi'
        assert result['disk_images']['cam_name'] == 'usb_cam.img'
        assert result['nested']['deeper']['key'] == 'value'

    def test_duplicate_top_level_key_raises(self):
        doc = "a: 1\nb: 2\na: 3\n"
        with pytest.raises(_DuplicateKeyError) as exc_info:
            yaml.load(doc, Loader=_DuplicateKeyDetectingLoader)
        # Error message must mention the duplicated key by name so
        # the operator can find it without guessing.
        assert "'a'" in str(exc_info.value)

    def test_duplicate_nested_key_raises(self):
        doc = """
        cloud_archive:
          shadow_pipeline_queue: false
          use_pipeline_reader: true
          shadow_pipeline_queue: true
        """
        with pytest.raises(_DuplicateKeyError) as exc_info:
            yaml.load(doc, Loader=_DuplicateKeyDetectingLoader)
        assert 'shadow_pipeline_queue' in str(exc_info.value)

    def test_error_includes_line_number(self):
        # Operator must be able to jump straight to the offending
        # line without grep.
        doc = "a: 1\nb: 2\nc: 3\nb: 4\n"
        with pytest.raises(_DuplicateKeyError) as exc_info:
            yaml.load(doc, Loader=_DuplicateKeyDetectingLoader)
        # Line 4 is where the duplicate 'b' appears (1-based count).
        # The error message includes "line 4".
        assert 'line 4' in str(exc_info.value), (
            "Error must include the line number of the duplicate "
            "so the operator can find it instantly. Got: %s" % exc_info.value
        )

    def test_actual_pi_bug_pattern(self):
        # Reproduce the exact shape of the May 18 bug on the Pi:
        # the same two keys appearing twice in the ``cloud_archive``
        # section, separated by a few hundred lines. The loader must
        # refuse this — silently keeping the last value is what
        # caused weeks of misconfigured behavior.
        doc = """
cloud_archive:
  enabled: true
  shadow_pipeline_queue: false
  use_pipeline_reader: true
  rclone_provider: gdrive
  rclone_remote: "tesla:"
  max_upload_mbps: 10
  shadow_pipeline_queue: true
  use_pipeline_reader: false
"""
        with pytest.raises(_DuplicateKeyError):
            yaml.load(doc, Loader=_DuplicateKeyDetectingLoader)

    def test_duplicate_in_deeply_nested_mapping_raises(self):
        doc = """
top:
  middle:
    inner:
      key1: 1
      key2: 2
      key1: 99
"""
        with pytest.raises(_DuplicateKeyError) as exc_info:
            yaml.load(doc, Loader=_DuplicateKeyDetectingLoader)
        assert "'key1'" in str(exc_info.value)

    def test_lists_with_repeated_string_values_are_fine(self):
        # Duplicates in a list are completely valid and must NOT
        # trigger the guard. Only duplicate MAPPING keys are bugs.
        doc = """
sync_folders:
  - SentryClips
  - SavedClips
  - SentryClips
"""
        result = yaml.load(doc, Loader=_DuplicateKeyDetectingLoader)
        assert result['sync_folders'] == [
            'SentryClips', 'SavedClips', 'SentryClips',
        ]

    def test_empty_mapping_loads_normally(self):
        # Edge case: empty document or empty inner mapping must load
        # without false positives.
        assert yaml.load("", Loader=_DuplicateKeyDetectingLoader) is None
        assert yaml.load("a: {}\n", Loader=_DuplicateKeyDetectingLoader) == {
            'a': {},
        }


class TestRepoConfigYamlLoadsClean:
    """Self-check: the in-repo ``config.yaml`` must itself be free of
    duplicate keys (otherwise gadget_web would refuse to boot in dev).
    """

    def test_repo_config_yaml_has_no_duplicate_keys(self):
        import os
        repo_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)),
        )
        config_path = os.path.join(repo_root, 'config.yaml')
        if not os.path.exists(config_path):
            pytest.skip("config.yaml not present in repo checkout")
        with open(config_path, 'r') as f:
            # Must not raise.
            yaml.load(f, Loader=_DuplicateKeyDetectingLoader)
