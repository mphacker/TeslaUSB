"""Regression test for the "Footage may have been lost" dismiss bug.

Issue history:
- #163 : add a Dismiss button to the home-page lost-footage banner.
- #167 / #170 : tombstone-on-server so a worker burst can't repopulate
  the dismissed banner.
- This bug : after PR #169 shipped the tombstone, the user reported the
  banner *still* did not disappear when Dismiss was clicked. Live
  reproduction with Playwright on cybertruckusb.local showed:
      element.hidden === true
      getComputedStyle(element).display === 'flex'
  The banner div carries an inline ``style="...display:flex;..."`` whose
  CSS specificity (1,0,0,0) defeats the UA stylesheet's
  ``[hidden] { display: none }`` rule (specificity 0,0,1,0). A scoped
  ``#files-lost-banner[hidden] { display: none !important; }`` rule
  was added to ``index.html`` to defeat the inline display.

This test pins that fix so a future template cleanup can't silently
re-introduce the bug. We do source-based assertions (reading
index.html as text) because rendering the full template requires an
app context with every blueprint registered — see ``test_settings_responsive.py``
for the same trade-off.
"""
from __future__ import annotations

import os
import re


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(
    REPO_ROOT, 'scripts', 'web', 'templates', 'index.html',
)


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestFilesLostBannerDismissCss:
    """The banner must be hideable via the ``hidden`` attribute."""

    def test_banner_element_exists(self):
        html = _read(INDEX_HTML)
        assert 'id="files-lost-banner"' in html

    def test_banner_has_inline_display_flex(self):
        """If this assertion ever flips to False, the
        ``[hidden] !important`` rule below is no longer required and
        the test below can be deleted. As long as inline ``display:flex``
        is present, the override rule is mandatory."""
        html = _read(INDEX_HTML)
        # Find the banner div tag and verify display:flex appears in
        # its inline style attribute.
        match = re.search(
            r'<div\s+id="files-lost-banner"[^>]*style="([^"]*)"',
            html,
            re.DOTALL,
        )
        assert match is not None, (
            "Could not find <div id=\"files-lost-banner\" ... style=\"...\">"
        )
        style_attr = match.group(1)
        has_inline_flex = re.search(r'display\s*:\s*flex', style_attr) is not None
        # We don't fail the test on either branch — this is a
        # diagnostic for the assertion below.
        assert has_inline_flex, (
            "Inline display:flex was removed from the banner. The "
            "[hidden] !important override rule is no longer strictly "
            "necessary; if you've moved the layout into a CSS class, "
            "delete this test or update it to match. See dismiss-bug "
            "history at the top of this file."
        )

    def test_hidden_attribute_overrides_inline_display(self):
        """Issue #170 follow-up — the dismiss bug.

        The CSS rule ``#files-lost-banner[hidden] { display: none !important; }``
        MUST be present in the template to defeat the inline
        ``display:flex`` style. Without it, ``banner.hidden = true``
        from JavaScript leaves the banner visible because inline-style
        specificity (1,0,0,0) beats the UA stylesheet's
        ``[hidden] { display: none }`` rule (0,0,1,0).

        The user-facing symptom: clicking the "Dismiss" button posts
        successfully (200 OK from ``/api/system/clear_lost_clips``) but
        the banner stays on screen — making the feature look broken.
        """
        html = _read(INDEX_HTML)
        # Allow whitespace flexibility but require the exact selector,
        # property, and !important.
        pattern = re.compile(
            r'#files-lost-banner\s*\[\s*hidden\s*\]\s*\{[^}]*'
            r'display\s*:\s*none\s*!important',
            re.DOTALL | re.IGNORECASE,
        )
        assert pattern.search(html), (
            "Missing CSS rule:\n"
            "    #files-lost-banner[hidden] { display: none !important; }\n"
            "This rule is REQUIRED to defeat the inline ``display:flex`` "
            "on the banner element. Without it, the JS Dismiss handler "
            "(which sets ``banner.hidden = true``) cannot actually hide "
            "the banner — see test docstring for the full bug history."
        )

    def test_dismiss_handler_still_sets_hidden_true(self):
        """Belt and suspenders — the JS handler is the other half of
        the fix. If a future refactor changes ``lostBanner.hidden = true``
        to e.g. ``lostBanner.style.display = 'none'``, the CSS rule is
        no longer strictly required and this whole file can be revisited.
        Today we still rely on the .hidden assignment, so verify it.
        """
        html = _read(INDEX_HTML)
        assert 'lostBanner.hidden = true' in html, (
            "The JS dismiss handler no longer sets "
            "``lostBanner.hidden = true`` — review whether the "
            "[hidden] !important CSS rule is still needed."
        )
