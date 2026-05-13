"""Tests for the Phase 5.9 (#102) Settings → Advanced sub-page.

Covers:

* ``GET  /settings/advanced/``       — page renders with current values
* ``POST /settings/advanced/save``   — atomic batched config.yaml write
* ``POST /settings/advanced/reset``  — single-tunable factory-default reset
* The ``_bounded_float`` / ``_bounded_int`` coercer security boundary
* The "no churn" no-op-save behavior (don't touch config.yaml when
  every value matches what is already on disk)
* Range validation rejects out-of-bounds inputs and aborts the WHOLE
  batch (not partial-success)
* The Advanced link is hidden by default — only reachable from the
  bottom of the main Settings page (no top-level nav item)

The blueprint reads ``config.*`` constants at request time inside
``_build_tunables`` so monkeypatching the ``config`` module in tests
exercises the real production code path without re-importing the
blueprint.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_updates():
    """Captures the dict passed to update_config_yaml so tests can
    assert on what would have been written without actually touching
    config.yaml on disk."""
    return {'last': None, 'calls': 0}


@pytest.fixture
def app(captured_updates, monkeypatch):
    from flask import Flask

    from blueprints.settings_advanced import settings_advanced_bp

    flask_app = Flask(
        __name__,
        template_folder='../scripts/web/templates',
    )
    flask_app.secret_key = 'test-secret'

    # Stub out get_base_context so we don't drag in mode_service /
    # partition probing during a unit test.
    import utils as utils_module
    monkeypatch.setattr(utils_module, 'get_base_context', lambda: {})

    # Register a dummy mode_control.index so url_for('mode_control.index')
    # in the template resolves cleanly.
    @flask_app.route('/settings/')
    def _stub_settings_index():
        return ''
    flask_app.view_functions['mode_control.index'] = _stub_settings_index

    flask_app.register_blueprint(settings_advanced_bp)

    def _capture(updates):
        captured_updates['last'] = dict(updates)
        captured_updates['calls'] += 1

    import helpers.config_updater as cu_module
    monkeypatch.setattr(cu_module, 'update_config_yaml', _capture, raising=False)
    # The blueprint imports update_config_yaml inside the route function,
    # so we don't need to patch it on blueprints.settings_advanced.

    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Bounded coercers — security boundary
# ---------------------------------------------------------------------------


class TestBoundedFloat:
    def test_in_range_passes(self):
        from blueprints.settings_advanced import _bounded_float
        cast = _bounded_float(0.0, 100.0)
        assert cast("42") == 42.0
        assert cast("0.0") == 0.0
        assert cast("100") == 100.0

    def test_below_min_rejected(self):
        from blueprints.settings_advanced import _bounded_float
        cast = _bounded_float(1.0, 5.0)
        with pytest.raises(ValueError, match="out of range"):
            cast("0.5")

    def test_above_max_rejected(self):
        from blueprints.settings_advanced import _bounded_float
        cast = _bounded_float(0.0, 100.0)
        with pytest.raises(ValueError, match="out of range"):
            cast("100.01")

    def test_garbage_raises_valueerror(self):
        from blueprints.settings_advanced import _bounded_float
        cast = _bounded_float(0.0, 100.0)
        with pytest.raises(ValueError):
            cast("not-a-number")


class TestBoundedInt:
    def test_in_range_passes(self):
        from blueprints.settings_advanced import _bounded_int
        cast = _bounded_int(1, 1000)
        assert cast("500") == 500

    def test_accepts_float_string(self):
        from blueprints.settings_advanced import _bounded_int
        cast = _bounded_int(1, 1000)
        assert cast("30.0") == 30

    def test_out_of_range(self):
        from blueprints.settings_advanced import _bounded_int
        cast = _bounded_int(1, 10)
        with pytest.raises(ValueError):
            cast("11")
        with pytest.raises(ValueError):
            cast("0")


# ---------------------------------------------------------------------------
# GET /settings/advanced/ — page renders
# ---------------------------------------------------------------------------


# Path to the template — checked directly because base.html drags in
# the entire url_for graph (mapping.map_view, ap_status, etc.) which
# would require stubbing every blueprint in the app to render in tests.
import os.path
_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'scripts', 'web', 'templates',
    'settings_advanced.html',
)


def _read_template() -> str:
    with open(_TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        return f.read()


class TestRender:
    def test_template_includes_persistent_banner(self):
        body = _read_template()
        # The exact banner copy is part of the contract — if you change
        # this string, also update the smoke test in the deploy step.
        assert 'Advanced settings affect background workers' in body

    def test_template_includes_back_link(self):
        body = _read_template()
        assert 'Back to Settings' in body
        assert "url_for('mode_control.index')" in body

    def test_template_iterates_tunables(self):
        body = _read_template()
        assert '{% for t in tunables %}' in body
        # Each row must wire form_name + label + description + min/max/step.
        for needle in [
            'name="{{ t.form_name }}"',
            '{{ t.label }}',
            '{{ t.description }}',
            'min="{{ t.min }}"',
            'max="{{ t.max }}"',
            'step="{{ t.step }}"',
        ]:
            assert needle in body, f"template missing: {needle}"

    def test_template_includes_reset_buttons(self):
        body = _read_template()
        assert 'Reset to default' in body
        assert 'resetAdvancedField' in body

    def test_template_save_form_posts_to_save_route(self):
        body = _read_template()
        assert "url_for('settings_advanced.save')" in body

    def test_template_extends_base(self):
        body = _read_template()
        assert '{% extends "base.html" %}' in body


class TestSettingsLink:
    """The Advanced page MUST be linked from the bottom of the main
    Settings page. NOT a top-level nav item — the spec requires it
    be a low-key link buried at the bottom."""

    def _index_html(self) -> str:
        path = os.path.join(
            os.path.dirname(__file__), '..', 'scripts', 'web',
            'templates', 'index.html',
        )
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    def test_index_includes_advanced_link(self):
        body = self._index_html()
        assert "url_for('settings_advanced.index')" in body, (
            "index.html (the main Settings page) must include a link "
            "to the Advanced sub-page"
        )
        assert 'Advanced settings' in body

    def test_advanced_link_NOT_in_main_nav(self):
        """base.html is the nav. The Advanced link must NOT be there."""
        path = os.path.join(
            os.path.dirname(__file__), '..', 'scripts', 'web',
            'templates', 'base.html',
        )
        with open(path, 'r', encoding='utf-8') as f:
            body = f.read()
        assert "settings_advanced.index" not in body, (
            "Advanced settings is intentionally NOT a top-level nav "
            "item — it should only be reachable from the bottom of "
            "the main Settings page"
        )


# ---------------------------------------------------------------------------
# Tunable schema invariants
# ---------------------------------------------------------------------------


class TestTunableSchema:
    def test_each_tunable_has_required_fields(self):
        from blueprints.settings_advanced import _build_tunables
        required = {
            'key', 'form_name', 'label', 'description',
            'current', 'default', 'unit',
            'min', 'max', 'step', 'cast',
        }
        for t in _build_tunables():
            missing = required - set(t.keys())
            assert not missing, f"tunable {t.get('label')} missing {missing}"

    def test_form_names_are_unique(self):
        from blueprints.settings_advanced import _build_tunables
        names = [t['form_name'] for t in _build_tunables()]
        assert len(names) == len(set(names)), "duplicate form_name"

    def test_keys_are_unique(self):
        from blueprints.settings_advanced import _build_tunables
        keys = [t['key'] for t in _build_tunables()]
        assert len(keys) == len(set(keys)), "duplicate config key"

    def test_default_within_min_max(self):
        from blueprints.settings_advanced import _build_tunables
        for t in _build_tunables():
            assert t['min'] <= t['default'] <= t['max'], (
                f"{t['label']} default {t['default']} outside "
                f"range [{t['min']}, {t['max']}]"
            )

    def test_specific_required_tunables_present(self):
        """Pin Phase 5.9 spec — these specific tunables MUST exist."""
        from blueprints.settings_advanced import _build_tunables
        keys = {t['key'] for t in _build_tunables()}
        required_keys = {
            'archive_queue.stable_write_age_seconds',
            'archive_queue.stale_claim_max_age_seconds',
            'archive_queue.inter_file_sleep_seconds',
            'archive_queue.load_pause_threshold',
            'archive_queue.load_pause_seconds',
            'archive_queue.chunk_pause_seconds',
            'archive_queue.per_file_time_budget_seconds',
            'mapping.index_too_new_seconds',
            'mapping.event_detection.speed_limit_mps',
        }
        missing = required_keys - keys
        assert not missing, f"Phase 5.9 spec violation: missing {missing}"


# ---------------------------------------------------------------------------
# POST /settings/advanced/save
# ---------------------------------------------------------------------------


class TestSave:
    def test_changed_value_persisted(self, client, captured_updates):
        # Use a value definitely different from default 5.0
        rv = client.post('/settings/advanced/save', data={
            'archive_stable_write_age_seconds': '7.5',
        }, follow_redirects=False)
        assert rv.status_code == 302
        assert captured_updates['calls'] == 1
        assert captured_updates['last'] == {
            'archive_queue.stable_write_age_seconds': 7.5,
        }

    def test_no_change_skips_yaml_write(self, client, captured_updates):
        """Don't touch config.yaml when the form value matches current."""
        from blueprints.settings_advanced import _build_tunables
        # Submit each tunable's CURRENT value (no actual change).
        form = {}
        for t in _build_tunables():
            form[t['form_name']] = str(t['current'])
        rv = client.post('/settings/advanced/save', data=form,
                         follow_redirects=False)
        assert rv.status_code == 302
        assert captured_updates['calls'] == 0, (
            "config.yaml should not be rewritten on no-op save"
        )

    def test_out_of_range_rejects_whole_batch(self, client, captured_updates):
        """A bad value aborts the entire submission (no partial save)."""
        rv = client.post('/settings/advanced/save', data={
            # Valid change
            'archive_stable_write_age_seconds': '8.0',
            # Bad: load_pause_threshold cap is 10.0
            'archive_load_pause_threshold': '999',
        }, follow_redirects=False)
        assert rv.status_code == 302
        assert captured_updates['calls'] == 0, (
            "out-of-range value must abort the whole save, not "
            "partially persist the valid field"
        )

    def test_garbage_value_rejected(self, client, captured_updates):
        rv = client.post('/settings/advanced/save', data={
            'archive_stable_write_age_seconds': 'not-a-number',
        }, follow_redirects=False)
        assert rv.status_code == 302
        assert captured_updates['calls'] == 0

    def test_unknown_field_ignored(self, client, captured_updates):
        """Stray form fields are ignored (don't 500 the route)."""
        rv = client.post('/settings/advanced/save', data={
            'totally_unknown_field': 'hello',
            'archive_stable_write_age_seconds': '6.0',
        }, follow_redirects=False)
        assert rv.status_code == 302
        # The known field still saved.
        assert captured_updates['calls'] == 1
        assert captured_updates['last'] == {
            'archive_queue.stable_write_age_seconds': 6.0,
        }


# ---------------------------------------------------------------------------
# POST /settings/advanced/reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_resets_single_tunable_to_default(self, client, captured_updates):
        rv = client.post('/settings/advanced/reset', data={
            'form_name': 'archive_stable_write_age_seconds',
        }, follow_redirects=False)
        assert rv.status_code == 302
        assert captured_updates['calls'] == 1
        assert captured_updates['last'] == {
            'archive_queue.stable_write_age_seconds': 5.0,
        }

    def test_unknown_form_name_does_not_write(self, client, captured_updates):
        rv = client.post('/settings/advanced/reset', data={
            'form_name': 'nonexistent_field',
        }, follow_redirects=False)
        assert rv.status_code == 302
        assert captured_updates['calls'] == 0

    def test_missing_form_name_does_not_write(self, client, captured_updates):
        rv = client.post('/settings/advanced/reset', data={},
                         follow_redirects=False)
        assert rv.status_code == 302
        assert captured_updates['calls'] == 0


# ---------------------------------------------------------------------------
# Integration: the runtime services actually read from config
# ---------------------------------------------------------------------------


class TestServicesReadFromConfig:
    """Tripwires that ensure the new config keys flow through to the
    code paths that consume them. If somebody rips out the config read
    and goes back to a hardcoded constant, these fail."""

    def test_archive_worker_reads_stable_write_age_from_config(self, monkeypatch):
        """``_stable_write_age_seconds()`` must reflect monkeypatched
        config, not the legacy module-level constant."""
        import importlib
        cfg = importlib.import_module('config')
        monkeypatch.setattr(cfg, 'ARCHIVE_QUEUE_STABLE_WRITE_AGE_SECONDS',
                            42.0, raising=False)

        from services.archive_worker import _stable_write_age_seconds
        assert _stable_write_age_seconds() == 42.0

    def test_archive_worker_falls_back_when_config_missing(self, monkeypatch):
        """If the config attribute is missing, the function returns
        the module-level fallback (NOT crashing the worker)."""
        import importlib
        cfg = importlib.import_module('config')
        monkeypatch.delattr(
            cfg, 'ARCHIVE_QUEUE_STABLE_WRITE_AGE_SECONDS', raising=False,
        )

        from services.archive_worker import (
            _stable_write_age_seconds, _STABLE_WRITE_AGE_SECONDS,
        )
        assert _stable_write_age_seconds() == _STABLE_WRITE_AGE_SECONDS

    def test_mapping_too_new_threshold_is_configurable(self):
        """Source-shape tripwire: the mapping_service too-new gate
        must NOT use a bare hardcoded 120 anymore — it has to read
        from config so the Advanced sub-page actually does something."""
        import inspect
        from services import mapping_service
        src = inspect.getsource(mapping_service.index_single_file)
        # Must reference the config constant.
        assert 'MAPPING_INDEX_TOO_NEW_SECONDS' in src, (
            "mapping_service.index_single_file lost its config import"
        )
        # Must NOT have the bare 120 literal in a comparison.
        assert ' < 120:' not in src, (
            "mapping_service.index_single_file still uses hardcoded 120 "
            "for the too-new gate; should read from config"
        )
