"""Phase 4.9 (#101) — iOS fullscreen toast in mapping.html.

The toast is a one-time, polite warning shown the FIRST time an iOS
Safari user taps the Fullscreen button on the video overlay. Because
iOS hands fullscreen off to its native player (which cannot have
HTML overlaid), the heads-up display (HUD) with map/speed/time
disappears in OS fullscreen — a Safari limitation we cannot
work around.

The fix:

1. Detect iOS Safari (incl. iPad on iOS 13+ desktop-mode masquerade).
2. On the FIRST fullscreen tap from an iOS browser, show the toast
   AND skip the fullscreen call. If we entered fullscreen the toast
   would be hidden behind the OS player — the user would never see
   the explanation.
3. Persist the "shown" flag in localStorage so we don't nag the user.
4. Subsequent taps proceed to ``webkitEnterFullscreen`` normally for
   users who deliberately want OS fullscreen and accept the HUD loss.

Because this is pure client-side JS that depends on browser APIs, we
verify the contract by reading mapping.html as text and pinning the
markers, branches, and sentinel keys.
"""

from __future__ import annotations

import os
import re

import pytest


_MAPPING_HTML = os.path.join(
    os.path.dirname(__file__),
    '..', 'scripts', 'web', 'templates', 'mapping.html',
)


@pytest.fixture(scope='module')
def mapping_html_text():
    with open(_MAPPING_HTML, 'r', encoding='utf-8') as f:
        return f.read()


# ---------------------------------------------------------------------------
# iOS detection contract
# ---------------------------------------------------------------------------


class TestIosDetection:

    def test_isios_safari_function_present(self, mapping_html_text):
        # Backwards-compatible alias preserved AND the canonical
        # `isIos` name introduced (review fix on PR #140).
        assert 'function isIos()' in mapping_html_text
        assert 'const isIosSafari = isIos' in mapping_html_text

    def test_detects_iphone_ipad_ipod(self, mapping_html_text):
        """The classic UA tokens (iPhone, iPad, iPod) must trigger
        detection on every iOS version."""
        assert '/iPhone|iPad|iPod/i' in mapping_html_text

    def test_detects_ipados_desktop_masquerade(self, mapping_html_text):
        """iPadOS 13+ identifies as Macintosh; the function must
        combine the Mac UA with maxTouchPoints to disambiguate iPad
        from a real Mac (which has zero touch points)."""
        block = mapping_html_text[
            mapping_html_text.index('function isIos()'):
        ][:1500]
        assert '/Macintosh/i' in block
        assert 'maxTouchPoints' in block

    def test_detection_is_defensive(self, mapping_html_text):
        """Must guard against `navigator` being undefined in headless
        / SSR contexts to avoid a ReferenceError in older renderers."""
        block = mapping_html_text[
            mapping_html_text.index('function isIos()'):
        ][:1500]
        assert "typeof navigator === 'undefined'" in block


# ---------------------------------------------------------------------------
# One-time toast contract
# ---------------------------------------------------------------------------


class TestOneTimeToast:

    def test_localstorage_key_is_stable(self, mapping_html_text):
        """The localStorage key must not change without a migration —
        otherwise users who saw the toast in one release will see it
        again after upgrade."""
        assert "IOS_FULLSCREEN_TOAST_KEY = 'iosFullscreenToastShown'" in (
            mapping_html_text
        )

    def test_has_shown_helper_present(self, mapping_html_text):
        assert 'function hasShownIosFullscreenToast()' in mapping_html_text

    def test_mark_helper_present(self, mapping_html_text):
        assert 'function markIosFullscreenToastShown()' in mapping_html_text

    def test_show_helper_present(self, mapping_html_text):
        assert 'function showIosFullscreenToast()' in mapping_html_text

    def test_localstorage_access_is_try_wrapped(self, mapping_html_text):
        """Private browsing or storage-disabled mode must not crash
        the fullscreen path. Both helpers must wrap localStorage in
        try/catch."""
        for fn_name in ('hasShownIosFullscreenToast',
                        'markIosFullscreenToastShown'):
            block_start = mapping_html_text.index('function ' + fn_name)
            block = mapping_html_text[block_start:block_start + 600]
            assert 'try {' in block, (
                f"{fn_name} must try-catch localStorage access"
            )
            assert 'catch' in block

    def test_in_memory_fallback_for_private_mode(self, mapping_html_text):
        """Safari Private Mode has localStorage quota=0 — setItem
        throws but getItem returns null. Without an in-memory flag the
        early return in overlayFullscreen() would fire on EVERY tap
        and the user could never reach webkitEnterFullscreen
        (softlocking the button despite the toast saying 'tap again').
        Pin the in-memory flag contract added in the PR #140 review
        fix.
        """
        # Module-scoped flag declaration present.
        assert '_iosFullscreenToastShownInMemory = false' in mapping_html_text

        # hasShown checks the in-memory flag FIRST so it short-circuits
        # before touching localStorage.
        has_block_start = mapping_html_text.index(
            'function hasShownIosFullscreenToast'
        )
        has_block = mapping_html_text[has_block_start:has_block_start + 800]
        in_memory_idx = has_block.index('_iosFullscreenToastShownInMemory')
        try_idx = has_block.index('try {')
        assert in_memory_idx < try_idx, (
            'hasShownIosFullscreenToast must check the in-memory flag '
            'BEFORE the try/localStorage block, otherwise Safari '
            'Private Mode will softlock the Fullscreen button.'
        )

        # mark…() sets the in-memory flag UNCONDITIONALLY (before the
        # try/catch around localStorage).
        mark_block_start = mapping_html_text.index(
            'function markIosFullscreenToastShown'
        )
        mark_block = mapping_html_text[mark_block_start:mark_block_start + 800]
        mark_set_idx = mark_block.index(
            '_iosFullscreenToastShownInMemory = true'
        )
        mark_try_idx = mark_block.index('try {')
        assert mark_set_idx < mark_try_idx, (
            'markIosFullscreenToastShown must set the in-memory flag '
            'BEFORE attempting localStorage.setItem, so Private Mode '
            'tap #2 succeeds even if setItem throws.'
        )

    def test_show_helper_falls_back_when_showtoast_missing(self,
                                                           mapping_html_text):
        """Defensive fallback: if a refactor moves showToast, the
        function must log to console rather than crashing."""
        block_start = mapping_html_text.index('function showIosFullscreenToast')
        block = mapping_html_text[block_start:block_start + 1200]
        assert "typeof showToast === 'function'" in block
        assert 'console.log' in block

    def test_toast_text_mentions_maximize(self, mapping_html_text):
        """The whole point of the toast is to redirect the user to
        the Maximize button. Pin the keyword."""
        block_start = mapping_html_text.index('function showIosFullscreenToast')
        block = mapping_html_text[block_start:block_start + 1200]
        assert 'Maximize' in block

    def test_toast_uses_info_severity(self, mapping_html_text):
        """Polite, informational — not a warning or error. 'info' is
        the existing showToast severity for non-actionable hints."""
        block_start = mapping_html_text.index('function showIosFullscreenToast')
        block = mapping_html_text[block_start:block_start + 1200]
        assert "showToast(msg, 'info')" in block


# ---------------------------------------------------------------------------
# Skip-fullscreen-on-first-tap contract
# ---------------------------------------------------------------------------


class TestFirstTapSkipsFullscreen:

    def test_first_tap_branch_returns_before_fullscreen(self,
                                                       mapping_html_text):
        """The whole point of the design is: on the first iOS tap we
        SHOW the toast and SKIP fullscreen, because entering OS
        fullscreen would hide the toast. Pin the early-return."""
        # Slice from overlayFullscreen() to its closing brace.
        start = mapping_html_text.index('function overlayFullscreen()')
        # End at the next "function " definition.
        rest = mapping_html_text[start:]
        # Function span is bounded; take ~3500 chars to be safe and
        # then trim at the next top-level "function ".
        end_offset = rest.find('\nfunction ', 50)
        if end_offset == -1:
            end_offset = 3500
        body = rest[:end_offset]

        # The iOS branch must contain BOTH showIosFullscreenToast and
        # an early return.
        assert 'isIos()' in body
        assert '!hasShownIosFullscreenToast()' in body
        assert 'showIosFullscreenToast()' in body
        assert 'markIosFullscreenToastShown()' in body
        # The early return MUST appear inside the iOS branch — i.e.
        # before the requestFullscreen / webkitEnterFullscreen calls.
        ios_branch_idx = body.index('isIos()')
        return_idx = body.index('return;', ios_branch_idx)
        fs_call_idx = body.index('requestFullscreen()')
        assert return_idx < fs_call_idx, (
            "The first-iOS-tap branch must `return;` before the "
            "fullscreen calls. Otherwise the toast is hidden behind "
            "the OS player."
        )

    def test_subsequent_taps_proceed_to_fullscreen(self, mapping_html_text):
        """Once the localStorage flag is set, the function must fall
        through to the original fullscreen call ladder. Pin all three
        branches stay intact."""
        start = mapping_html_text.index('function overlayFullscreen()')
        body = mapping_html_text[start:start + 4000]
        assert 'stage.requestFullscreen' in body
        assert 'stage.webkitRequestFullscreen' in body
        assert 'v.webkitEnterFullscreen' in body
