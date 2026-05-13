"""Phase 4.8 (#101) — system-health status dot in the nav bar.

The dot is a small element in ``base.html`` that polls
``/api/system/health`` every 30 s and colours itself per the
``overall.severity`` field. Because rendering ``base.html`` end-to-end
requires every blueprint in the app to be registered (``url_for``
dependencies), these tests verify the contract by reading the
template + CSS as text and asserting the markers, classes, and JS
glue are present.

The deploy smoke test uses ``grep -c 'data-status-dot' >= 1`` on the
rendered home page to confirm the live element makes it to the wire.
"""

from __future__ import annotations

import os
import re

import pytest


_BASE_HTML = os.path.join(
    os.path.dirname(__file__),
    '..', 'scripts', 'web', 'templates', 'base.html',
)
_STYLE_CSS = os.path.join(
    os.path.dirname(__file__),
    '..', 'scripts', 'web', 'static', 'css', 'style.css',
)


@pytest.fixture(scope='module')
def base_html_text():
    with open(_BASE_HTML, 'r', encoding='utf-8') as f:
        return f.read()


@pytest.fixture(scope='module')
def style_css_text():
    with open(_STYLE_CSS, 'r', encoding='utf-8') as f:
        return f.read()


# ---------------------------------------------------------------------------
# Template marker contract — these are what the smoke test greps for
# ---------------------------------------------------------------------------


class TestStatusDotMarkers:

    def test_data_status_dot_marker_present(self, base_html_text):
        """The deploy smoke test runs ``grep -c 'data-status-dot' >= 1``
        against the rendered page. If we ever rename or remove the
        attribute the smoke test will fail silently — pin the marker
        in source so the unit test catches the rename first."""
        assert 'data-status-dot="health"' in base_html_text

    def test_health_dot_id_present(self, base_html_text):
        """The poller targets ``getElementById('health-dot')``. Renaming
        breaks the colour update."""
        assert 'id="health-dot"' in base_html_text

    def test_health_dot_link_id_present(self, base_html_text):
        """The poller toggles ``link.hidden`` on the wrapper <a>. The
        wrapper carries the tap target (44 × 44 hit area) and the
        tooltip — losing it makes the dot inaccessible."""
        assert 'id="health-dot-link"' in base_html_text

    def test_health_dot_link_initially_hidden(self, base_html_text):
        """We MUST NOT flash a stale colour on first paint. Hidden
        attribute on the link wrapper guarantees the dot is invisible
        until the first poll completes."""
        # Find the line containing id="health-dot-link" and assert
        # `hidden` appears before the closing > of that opening tag.
        m = re.search(
            r'<a[^>]*id="health-dot-link"[^>]*>',
            base_html_text,
        )
        assert m is not None, "<a id='health-dot-link'> not found"
        assert ' hidden' in m.group(0), (
            "health-dot-link must have the `hidden` attribute on initial "
            "render so we don't flash a stale colour"
        )


class TestStatusDotPollingContract:

    def test_polls_system_health_endpoint(self, base_html_text):
        """The dot reads from /api/system/health (Phase 4.2 endpoint).
        Reusing the same endpoint as the Settings system-health card
        is intentional — single source of truth."""
        assert "fetch('/api/system/health'" in base_html_text

    def test_severity_class_map_present(self, base_html_text):
        """All four severity values must map to a CSS class. A missing
        entry would leave the dot stuck on the previous colour when the
        unmapped severity is returned."""
        assert "ok:" in base_html_text and "'health-dot-ok'" in base_html_text
        assert "warn:" in base_html_text and "'health-dot-warn'" in base_html_text
        assert "error:" in base_html_text and "'health-dot-error'" in base_html_text
        assert "unknown:" in base_html_text and "'health-dot-unknown'" in base_html_text

    def test_poll_interval_under_one_minute(self, base_html_text):
        """Poll interval lives in const POLL_MS. Anything > 60s would
        make the dot feel stale; anything < 5s would beat up the cached
        probe (WiFi/AP shellouts have a 30 s TTL — sub-30s polls would
        hit the unique-name in-flight lock more often than necessary
        and could starve other consumers)."""
        m = re.search(r'const POLL_MS = (\d+);', base_html_text)
        assert m is not None, "POLL_MS const not found"
        ms = int(m.group(1))
        assert 5000 <= ms <= 60000, f"POLL_MS out of band: {ms}ms"


class TestStatusDotCss:

    def test_all_four_severity_classes_styled(self, style_css_text):
        """If any severity class is missing a CSS rule the dot falls
        through to the bare .health-dot rule (transparent background)
        and is invisible. Pin all four."""
        for sev in ('ok', 'warn', 'error', 'unknown'):
            cls = f'.status-dot.health-dot.health-dot-{sev}'
            assert cls in style_css_text, f"missing CSS rule for {cls}"

    def test_uses_design_system_tokens_with_fallbacks(self, style_css_text):
        """Per design-system rules every accent reference must include
        an inline hex fallback so ``--accent-info``-style undefined-var
        regressions don't recur (see PR #138 review). Verify the four
        health-dot variants use ``var(--ds-accent-X, #...)`` shape."""
        # Slice just the health-dot block to avoid matching unrelated rules.
        block = style_css_text[
            style_css_text.index('Phase 4.8 (#101)'):
        ]
        # Trim to first ~3 KB after the header to scope the assertion.
        block = block[:3500]
        for token in ('--ds-accent-success',
                      '--ds-accent-warning',
                      '--ds-accent-danger'):
            # Each token must be referenced WITH a fallback hex.
            pattern = (
                r'var\(' + re.escape(token) +
                r',\s*#[0-9A-Fa-f]{3,8}\)'
            )
            assert re.search(pattern, block), (
                f"{token} reference missing inline hex fallback"
            )

    def test_link_has_44px_minimum_tap_target(self, style_css_text):
        """Design-system rule: interactive elements need ≥ 44 × 44 px
        tap targets. The dot itself is 10 px; the wrapper <a> must
        provide the buffer."""
        block = style_css_text[
            style_css_text.index('.health-dot-link'):
        ][:600]
        assert 'min-width: 44px' in block
        assert 'min-height: 44px' in block

    def test_no_hardcoded_emoji(self, style_css_text):
        """Design-system rule: no emoji. The CSS block shouldn't
        embed any U+1F000–U+1FFFF or U+2600–U+27BF characters."""
        block = style_css_text[
            style_css_text.index('Phase 4.8 (#101)'):
        ][:3500]
        for ch in block:
            o = ord(ch)
            assert not (0x2600 <= o <= 0x27BF), (
                f"unexpected emoji codepoint U+{o:04X} in health-dot CSS"
            )
            assert not (0x1F000 <= o <= 0x1FFFF), (
                f"unexpected emoji codepoint U+{o:04X} in health-dot CSS"
            )
