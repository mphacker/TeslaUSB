"""Tests for the Phase 3a.2 (#98) one-shot migration of legacy
``cleanup_config.json`` into the unified ``config.yaml`` ``cleanup``
section.

The migration must be idempotent, never raise, only seed allow-listed
folder names, and rename the legacy file with a ``.migrated`` suffix
on success so the next boot doesn't redo the work.
"""

from __future__ import annotations

import json

import pytest
import yaml

from services.cleanup_service import migrate_legacy_cleanup_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gadget_dir(tmp_path):
    return tmp_path


@pytest.fixture
def config_yaml_path(gadget_dir):
    p = gadget_dir / 'config.yaml'
    return str(p)


def _write_yaml(path, data):
    with open(path, 'w') as f:
        yaml.safe_dump(data, f)


def _write_legacy(gadget_dir, data):
    p = gadget_dir / 'cleanup_config.json'
    p.write_text(json.dumps(data))
    return p


def _read_yaml(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    """Pin the migration contract end-to-end."""

    def test_no_legacy_file_is_noop(self, gadget_dir, config_yaml_path):
        _write_yaml(config_yaml_path, {'cleanup': {}})
        summary = migrate_legacy_cleanup_config(
            str(gadget_dir), config_yaml_path=config_yaml_path,
        )
        assert summary['migrated'] is False
        assert 'no legacy file' in summary['reason']
        # No .migrated file should appear because none existed.
        assert not (gadget_dir / 'cleanup_config.json.migrated').exists()

    def test_legacy_imported_into_yaml(self, gadget_dir, config_yaml_path):
        _write_yaml(config_yaml_path, {'cleanup': {}})
        legacy = _write_legacy(gadget_dir, {
            'SentryClips': {
                'enabled': True,
                'age_based': {'days': 90, 'enabled': True},
            },
            'SavedClips': {
                'enabled': False,
                'age_based': {'days': 365, 'enabled': True},
            },
        })
        summary = migrate_legacy_cleanup_config(
            str(gadget_dir), config_yaml_path=config_yaml_path,
        )
        assert summary['migrated'] is True
        assert sorted(summary['imported_folders']) == ['SavedClips', 'SentryClips']
        cfg = _read_yaml(config_yaml_path)
        policies = cfg['cleanup']['policies']
        assert policies['SentryClips']['retention_days'] == 90
        assert policies['SentryClips']['enabled'] is True
        assert policies['SavedClips']['retention_days'] == 365
        assert policies['SavedClips']['enabled'] is False
        # Defaults seeded so the watchdog has something to fall back on.
        assert cfg['cleanup']['default_retention_days'] == 30
        assert cfg['cleanup']['free_space_target_pct'] == 10
        # Legacy file renamed.
        assert not legacy.exists()
        assert (gadget_dir / 'cleanup_config.json.migrated').exists()

    def test_idempotent_when_yaml_already_has_policies(self, gadget_dir, config_yaml_path):
        # User has already used the new UI to set policies. The
        # legacy file (e.g. left behind from a downgrade) MUST NOT
        # overwrite the user's choices.
        _write_yaml(config_yaml_path, {
            'cleanup': {
                'policies': {
                    'ArchivedClips': {'enabled': True, 'retention_days': 14},
                },
            },
        })
        legacy = _write_legacy(gadget_dir, {
            'SentryClips': {
                'enabled': True, 'age_based': {'days': 365, 'enabled': True},
            },
        })
        summary = migrate_legacy_cleanup_config(
            str(gadget_dir), config_yaml_path=config_yaml_path,
        )
        assert summary['migrated'] is False
        assert 'already populated' in summary['reason']
        cfg = _read_yaml(config_yaml_path)
        # The user's existing override is preserved untouched.
        assert cfg['cleanup']['policies']['ArchivedClips']['retention_days'] == 14
        assert 'SentryClips' not in cfg['cleanup']['policies']
        # Legacy file still gets renamed so subsequent boots don't keep
        # re-checking it.
        assert not legacy.exists()
        assert (gadget_dir / 'cleanup_config.json.migrated').exists()

    def test_drops_unknown_folder_names(self, gadget_dir, config_yaml_path):
        _write_yaml(config_yaml_path, {'cleanup': {}})
        _write_legacy(gadget_dir, {
            'BogusFolder': {'enabled': True, 'age_based': {'days': 30, 'enabled': True}},
            'SentryClips': {'enabled': True, 'age_based': {'days': 90, 'enabled': True}},
        })
        summary = migrate_legacy_cleanup_config(
            str(gadget_dir), config_yaml_path=config_yaml_path,
        )
        assert summary['migrated'] is True
        cfg = _read_yaml(config_yaml_path)
        assert 'SentryClips' in cfg['cleanup']['policies']
        assert 'BogusFolder' not in cfg['cleanup']['policies']

    def test_skips_policies_without_age_days(self, gadget_dir, config_yaml_path):
        # A legacy entry that's enabled but has no usable retention
        # value would be ambiguous — silently skip it rather than
        # persist a junk row.
        _write_yaml(config_yaml_path, {'cleanup': {}})
        _write_legacy(gadget_dir, {
            'RecentClips': {'enabled': True, 'age_based': {'days': 0}},
            'SavedClips': {'enabled': True},   # no age_based at all
            'SentryClips': {'enabled': True, 'age_based': {'days': 60}},
        })
        summary = migrate_legacy_cleanup_config(
            str(gadget_dir), config_yaml_path=config_yaml_path,
        )
        assert summary['migrated'] is True
        cfg = _read_yaml(config_yaml_path)
        assert list(cfg['cleanup']['policies'].keys()) == ['SentryClips']

    def test_unparseable_legacy_file_does_not_raise(self, gadget_dir, config_yaml_path):
        _write_yaml(config_yaml_path, {'cleanup': {}})
        (gadget_dir / 'cleanup_config.json').write_text('{not json,,}')
        summary = migrate_legacy_cleanup_config(
            str(gadget_dir), config_yaml_path=config_yaml_path,
        )
        assert summary['migrated'] is False
        assert 'unparseable' in summary['reason']
        # config.yaml is untouched.
        cfg = _read_yaml(config_yaml_path)
        assert cfg.get('cleanup') == {}

    def test_unreadable_yaml_does_not_raise(self, gadget_dir):
        # Pointing at a path that doesn't exist must produce a
        # graceful failure summary, not a crash.
        _write_legacy(gadget_dir, {
            'SentryClips': {'enabled': True, 'age_based': {'days': 90}},
        })
        summary = migrate_legacy_cleanup_config(
            str(gadget_dir),
            config_yaml_path=str(gadget_dir / 'nonexistent-config.yaml'),
        )
        assert summary['migrated'] is False
        # Either "config.yaml unreadable" (open failed) or "no legacy
        # file" if the open succeeded against an empty file — both are
        # graceful outcomes.
        assert summary['reason']  # non-empty string

    def test_no_migratable_policies_renames_legacy(self, gadget_dir, config_yaml_path):
        # Legacy file exists but contains only entries we'd skip
        # (zero days, junk folder names). Migration should rename the
        # file so subsequent boots don't keep re-examining it.
        _write_yaml(config_yaml_path, {'cleanup': {}})
        _write_legacy(gadget_dir, {
            'BogusFolder': {'enabled': True, 'age_based': {'days': 30}},
            'SentryClips': {'enabled': True, 'age_based': {'days': 0}},
        })
        summary = migrate_legacy_cleanup_config(
            str(gadget_dir), config_yaml_path=config_yaml_path,
        )
        assert summary['migrated'] is False
        assert 'no migratable policies' in summary['reason']
        assert not (gadget_dir / 'cleanup_config.json').exists()
        assert (gadget_dir / 'cleanup_config.json.migrated').exists()
