"""Issue #125 — Settings cards: mobile responsiveness + 44x44 touch targets.

Per ``docs/UI_UX_DESIGN_SYSTEM.md``:
  * Touch targets MUST be >= 44x44 px (L232, L263, L414).
  * Form inputs MUST have ``min-height: 44px`` and ``font-size: 16px``
    (the latter prevents iOS auto-zoom).
  * The 375 px viewport (mobile) MUST collapse multi-column form
    grids to a single column.

These tests pin the index.html Settings card markup and the new
shared CSS classes in ``static/css/style.css`` so future template
edits can't silently regress mobile responsiveness or touch
targets.

We use a source-based test (read template + CSS as text) because
rendering ``index.html`` requires a complete Flask app context with
all blueprints registered (see PR #149 / settings_advanced tests
for the same trade-off).
"""
from __future__ import annotations

import os
import re

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(
    REPO_ROOT, 'scripts', 'web', 'templates', 'index.html',
)
STYLE_CSS = os.path.join(
    REPO_ROOT, 'scripts', 'web', 'static', 'css', 'style.css',
)


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


# ---------------------------------------------------------------------------
# CSS-side: the new utility classes must exist with the right rules.
# ---------------------------------------------------------------------------


class TestCssUtilityClasses:

    def test_settings_form_grid_class_exists(self):
        css = _read(STYLE_CSS)
        assert '.settings-form-grid' in css, (
            "Missing .settings-form-grid utility class. Required for "
            "consistent two-column form layout in Settings cards."
        )

    def test_settings_form_grid_has_mobile_collapse_breakpoint(self):
        """At 600 px and below, grid MUST collapse to a single column."""
        css = _read(STYLE_CSS)
        # Look for the media query + grid-template-columns:1fr
        # combination, allowing whitespace variation.
        pattern = re.compile(
            r'@media\s*\([^)]*max-width:\s*600px[^)]*\)\s*\{'
            r'[^}]*\.settings-form-grid\s*\{[^}]*'
            r'grid-template-columns:\s*1fr[^}]*\}',
            re.DOTALL,
        )
        assert pattern.search(css), (
            "Missing @media (max-width: 600px) rule that collapses "
            ".settings-form-grid to grid-template-columns:1fr. "
            "Required by design system mobile-L breakpoint (375px)."
        )

    def test_settings_form_input_class_exists(self):
        css = _read(STYLE_CSS)
        assert '.settings-form-input' in css

    def test_settings_form_input_meets_44px_touch_target(self):
        """Pin the touch-target height. Per design system L232 + L263:
        ``min-height: 44px`` is the floor for interactive elements."""
        css = _read(STYLE_CSS)
        # Find the .settings-form-input block and check min-height.
        match = re.search(
            r'\.settings-form-input\s*\{([^}]+)\}',
            css, re.DOTALL,
        )
        assert match, "Could not locate .settings-form-input block"
        block = match.group(1)
        mh = re.search(r'min-height:\s*(\d+)px', block)
        assert mh, ".settings-form-input missing min-height declaration"
        assert int(mh.group(1)) >= 44, (
            f"Touch target {mh.group(1)}px below design system "
            f"minimum of 44px"
        )

    def test_settings_form_input_uses_existing_tokens(self):
        """The new class must use CSS tokens that ACTUALLY exist in
        both light and dark themes — otherwise borders silently
        disappear (the original bug). Allowed border tokens:
        ``--border-color``, ``--border-input``."""
        css = _read(STYLE_CSS)
        match = re.search(
            r'\.settings-form-input\s*\{([^}]+)\}',
            css, re.DOTALL,
        )
        block = match.group(1)
        # Must NOT use the broken --border (alone) token.
        assert 'var(--border)' not in block, (
            ".settings-form-input uses the broken --border token "
            "(no such token in style.css). Use --border-input or "
            "--border-color instead."
        )
        # Must use a known-existing token.
        assert ('var(--border-input)' in block or
                'var(--border-color)' in block), (
            ".settings-form-input border must use --border-input "
            "or --border-color (both exist in :root and "
            "[data-theme='dark'])."
        )

    def test_settings_form_input_has_16px_font(self):
        """Prevent iOS auto-zoom on focus. Design system spec L264."""
        css = _read(STYLE_CSS)
        match = re.search(
            r'\.settings-form-input\s*\{([^}]+)\}',
            css, re.DOTALL,
        )
        block = match.group(1)
        # Either inline 16px or relying on the global rule that
        # already covers all input types.
        global_rule = re.search(
            r'input\[type="number"\][^{]*\{[^}]*font-size:\s*16px',
            css, re.DOTALL,
        )
        explicit_16 = re.search(r'font-size:\s*16px', block)
        assert global_rule or explicit_16, (
            "iOS auto-zoom protection missing — input must have "
            "font-size: 16px (either via .settings-form-input or "
            "the global input[type='number'] rule)."
        )

    def test_settings_form_policy_row_collapses_on_mobile(self):
        css = _read(STYLE_CSS)
        # The 3-column policy row also needs a mobile collapse.
        pattern = re.compile(
            r'@media\s*\([^)]*max-width:\s*600px[^)]*\)\s*\{'
            r'[^}]*\.settings-form-policy-row\s*\{[^}]*'
            r'grid-template-columns:\s*1fr[^}]*\}',
            re.DOTALL,
        )
        assert pattern.search(css), (
            "Missing mobile-collapse rule for .settings-form-policy-row"
        )


# ---------------------------------------------------------------------------
# Template-side: index.html must use the new classes (not inline styles).
# ---------------------------------------------------------------------------


class TestIndexHtmlAdoption:

    def test_no_legacy_six_six_padding_in_settings_inputs(self):
        """Inline ``padding:6px 10px`` (the 30 px-tall input bug)
        must not appear in any of the Settings card form inputs."""
        html = _read(INDEX_HTML)
        # Scope: only count occurrences inside the settings-section
        # area (skip nav buttons, etc.). Easiest heuristic — count
        # specifically the bug shape.
        pattern = re.compile(
            r'padding:\s*6px\s+10px;\s*border:\s*1px\s+solid\s+'
            r'var\(--border\)',
        )
        matches = pattern.findall(html)
        assert len(matches) == 0, (
            f"Found {len(matches)} legacy 'padding:6px 10px; "
            f"border:1px solid var(--border)' input style(s) — "
            f"these are the original 30px-tall inputs with broken "
            f"borders. Replace with class='settings-form-input'."
        )

    def test_storage_retention_card_uses_grid_class(self):
        """The four Storage & Retention number inputs must live in
        elements using ``class="settings-form-grid"``."""
        html = _read(INDEX_HTML)
        # The card has TWO grid wrappers (default+free-space, then
        # max-archive+short-warning).
        # Anchor: between '<form id="storage-retention-form"' and
        # '</form>' there should be at least 2 occurrences.
        m = re.search(
            r'<form\s+id="storage-retention-form".*?</form>',
            html, re.DOTALL,
        )
        assert m, "Storage & Retention form block not found"
        form_html = m.group(0)
        grid_count = form_html.count('class="settings-form-grid"')
        assert grid_count >= 2, (
            f"Storage & Retention form must use settings-form-grid "
            f"on both row wrappers; found {grid_count}."
        )
        input_count = form_html.count('class="settings-form-input"')
        assert input_count >= 4, (
            f"Storage & Retention form must use settings-form-input "
            f"on all 4 number inputs; found {input_count}."
        )

    def test_archive_settings_card_uses_classes(self):
        html = _read(INDEX_HTML)
        # The Archive Settings card has 1 settings-form-grid + 2 inputs.
        # Locate by its <details> wrapper.
        m = re.search(
            r'<!--\s*Archive Settings\s*-->.*?</details>',
            html, re.DOTALL,
        )
        assert m, "Archive Settings card block not found"
        block = m.group(0)
        assert 'class="settings-form-grid"' in block, (
            "Archive Settings card not using settings-form-grid"
        )
        assert block.count('class="settings-form-input"') >= 2, (
            "Archive Settings inputs not using settings-form-input"
        )

    def test_mapping_settings_card_uses_classes(self):
        html = _read(INDEX_HTML)
        assert 'name="trip_gap_minutes"' in html, "test fixture broken"
        # Find the trip_gap_minutes input and verify it has the class.
        # The form-group wrapper is between the grid div and the input,
        # so we just assert proximity: the class appears within ~500
        # chars before the input.
        idx = html.index('name="trip_gap_minutes"')
        window = html[max(0, idx - 800):idx]
        assert 'class="settings-form-grid"' in window, (
            "trip_gap_minutes not nested under a "
            "<div class='settings-form-grid'> within 800 chars upstream"
        )
        # And the input itself uses settings-form-input.
        # Find the start of this <input ...> and check forward 300 chars.
        input_start = html.rfind('<input', 0, idx)
        input_block = html[input_start:idx + 200]
        assert 'class="settings-form-input"' in input_block, (
            "trip_gap_minutes input missing class='settings-form-input'"
        )

    def test_samba_password_input_uses_class(self):
        html = _read(INDEX_HTML)
        assert 'name="samba_password"' in html, "test fixture broken"
        # The samba password input should have settings-form-input.
        # It also needs flex:1 from the parent flex layout, so we
        # accept BOTH a class= attribute AND the flex inline.
        pattern = re.compile(
            r'<input[^>]*name="samba_password"[^>]*'
            r'class="settings-form-input"',
        )
        assert pattern.search(html), (
            "Samba password input must use class='settings-form-input'"
        )

    def test_no_emoji_in_auto_cleanup_action_card(self):
        """Design system: no emoji icons. Auto-Cleanup card used
        broom 🧹 emoji — must be replaced with a Lucide SVG."""
        html = _read(INDEX_HTML)
        # Locate the Auto-Cleanup section.
        m = re.search(
            r'<!--\s*Storage Maintenance\s*-->.*?</details>',
            html, re.DOTALL,
        )
        assert m, "Auto-Cleanup section not found"
        block = m.group(0)
        # 🧹 is U+1F9F9; Python sees it directly.
        assert '🧹' not in block, (
            "Broom emoji 🧹 still present in Auto-Cleanup card. "
            "Use a Lucide SVG icon (e.g., #icon-sparkles) instead."
        )
        # Old → arrow: replaced with a Lucide chevron.
        # The original was a literal arrow character.
        assert ('icon-sparkles' in block or
                'icon-trash-2' in block or
                'lucide-sprite.svg' in block), (
            "Auto-Cleanup card must use a Lucide SVG icon"
        )


# ---------------------------------------------------------------------------
# Cross-cutting: every Settings card touched must have NO 'var(--border)'
# (alone) anywhere — that token doesn't exist.
# ---------------------------------------------------------------------------


class TestNoBrokenBorderToken:

    def test_no_solo_border_token_in_settings_inputs(self):
        """The bare ``var(--border)`` token (no fallback) must NOT
        appear in any new edit. The codebase's existing token set
        is ``--border-color``, ``--border-light``, ``--border-table``,
        ``--border-input``. Bare ``var(--border)`` falls through to
        ``border-color: initial`` and renders invisible in dark mode.

        This test scopes to occurrences that are NOT followed by a
        legitimate CSS fallback like ``var(--border-color, var(--border))``
        — those are defensive and OK.
        """
        html = _read(INDEX_HTML)
        bad = []
        for line_num, line in enumerate(html.split('\n'), start=1):
            if 'var(--border)' not in line:
                continue
            # Skip lines that USE --border as a fallback — that's the
            # defensive pattern and is fine.
            # Pattern: ``var(--SOMETHING, var(--border))``
            if re.search(r'var\(--[a-z-]+,\s*var\(--border\)\)', line):
                continue
            bad.append((line_num, line.strip()))

        # Pre-fix: we expected 11 of these. After this PR the count
        # should be 0 in the Settings cards we audited (and in fact
        # 0 anywhere that's not behind a defensive fallback).
        assert len(bad) == 0, (
            f"Found {len(bad)} solo var(--border) usage(s) — this "
            f"token doesn't exist in style.css. Use --border-color "
            f"or --border-input instead.\n"
            + "\n".join(f"  L{ln}: {txt[:100]}" for ln, txt in bad[:10])
        )
