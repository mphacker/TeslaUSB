"""Issue #127 — _refresh_ro_mount must use drop_caches=2, not umount+mount.

Pre-fix, ``mapping_service._refresh_ro_mount`` invalidated the kernel
VFS cache by ``umount + mount -o ro`` on the present-mode RO mount.
Per ``.github/copilot-instructions.md`` (Mount Safety section), that
pattern is forbidden: any disruption of the present-mode RO mount can
race with Tesla's gadget reads and cause a transient I/O error,
losing footage if Tesla is actively recording.

The kernel-supported replacement is ``echo 2 > /proc/sys/vm/drop_caches``
(slabs only — dentry + inode cache). It does NOT touch the mount,
loop device, image file, or gadget binding.

These tests pin the new contract:

1. ``_refresh_ro_mount`` issues exactly one subprocess call.
2. That call writes ``"2\n"`` to ``/proc/sys/vm/drop_caches`` via
   ``sudo tee`` (the standard pattern for writing to root-owned
   /proc files from an unprivileged process).
3. NO ``umount``, ``mount``, ``findmnt``, or ``nsenter`` invocation.
4. The ``current_mode() != 'present'`` early return is preserved.
5. A subprocess failure is non-fatal (legacy contract preserved).
"""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def _force_present_mode(monkeypatch):
    """Force ``current_mode()`` to return 'present' so the function
    actually executes its body (vs. early-returning)."""
    import services.mode_service as ms

    monkeypatch.setattr(ms, 'current_mode', lambda: 'present')
    yield


class TestRefreshRoMount:

    def test_uses_drop_caches_via_sudo_tee(self, _force_present_mode):
        """The function MUST write to /proc/sys/vm/drop_caches via
        ``sudo tee`` — the standard pattern for writing to a
        root-owned /proc file from an unprivileged process."""
        from services.mapping_service import _refresh_ro_mount

        with patch('services.mapping_service.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _refresh_ro_mount("/mnt/gadget/part1-ro/TeslaCam")

        assert mock_run.call_count == 1, (
            f"Expected exactly 1 subprocess call (the slab-cache "
            f"invalidation), got {mock_run.call_count}"
        )
        args, kwargs = mock_run.call_args
        cmd = args[0] if args else kwargs.get('args')

        # Command shape must be ['sudo', 'tee', '/proc/sys/vm/drop_caches'].
        assert cmd == ["sudo", "tee", "/proc/sys/vm/drop_caches"], (
            f"Wrong command shape: {cmd}. Expected the slab-cache "
            f"invalidation pattern from copilot-instructions.md."
        )

        # Input must be exactly "2\n" (drop slab caches only).
        assert kwargs.get('input') == "2\n", (
            f"Expected input='2\\n' (slab-only invalidation), got "
            f"{kwargs.get('input')!r}. drop_caches=1 invalidates page "
            f"cache (heavy); =2 invalidates slabs only (cheap, what "
            f"we want); =3 invalidates both."
        )

    def test_does_not_call_umount_or_mount(self, _force_present_mode):
        """The function MUST NOT call umount, mount, findmnt, or
        wrap any command in ``nsenter``. Those are exactly the
        forbidden patterns this PR is removing."""
        from services.mapping_service import _refresh_ro_mount

        with patch('services.mapping_service.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _refresh_ro_mount("/mnt/gadget/part1-ro/TeslaCam")

        for call in mock_run.call_args_list:
            args, _ = call
            cmd = args[0] if args else []
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else str(cmd)
            for forbidden in ('umount', 'mount -o', 'findmnt', 'nsenter'):
                assert forbidden not in cmd_str, (
                    f"FORBIDDEN command pattern '{forbidden}' found in "
                    f"_refresh_ro_mount: {cmd_str}. This is exactly "
                    f"what issue #127 forbids — Tesla may be recording "
                    f"and umount/remount of the gadget-shared RO mount "
                    f"loses footage."
                )

    def test_early_returns_in_edit_mode(self, monkeypatch):
        """When mode is 'edit', the call must be a no-op (no
        subprocess invocation at all). The local mount IS the write
        path in edit mode, so the cache is fresh by definition."""
        import services.mode_service as ms
        from services.mapping_service import _refresh_ro_mount

        monkeypatch.setattr(ms, 'current_mode', lambda: 'edit')

        with patch('services.mapping_service.subprocess.run') as mock_run:
            _refresh_ro_mount("/mnt/gadget/part1/TeslaCam")

        assert mock_run.call_count == 0, (
            f"Expected 0 subprocess calls in edit mode, got "
            f"{mock_run.call_count}. Edit-mode is a no-op."
        )

    def test_early_returns_in_unknown_mode(self, monkeypatch):
        """Unknown mode (e.g. transitioning) is also a no-op."""
        import services.mode_service as ms
        from services.mapping_service import _refresh_ro_mount

        monkeypatch.setattr(ms, 'current_mode', lambda: 'transitioning')

        with patch('services.mapping_service.subprocess.run') as mock_run:
            _refresh_ro_mount("/mnt/gadget/part1-ro/TeslaCam")

        assert mock_run.call_count == 0

    def test_subprocess_failure_is_non_fatal(self, _force_present_mode, caplog):
        """``CalledProcessError`` from the slab-cache write is logged
        as a warning but never re-raised. All callers wrap this in
        try/except too, but defense-in-depth: it must NOT raise."""
        import logging
        from services.mapping_service import _refresh_ro_mount

        caplog.set_level(logging.WARNING)
        with patch('services.mapping_service.subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1, cmd=["sudo", "tee", "/proc/sys/vm/drop_caches"],
            )
            # Must NOT raise.
            _refresh_ro_mount("/mnt/gadget/part1-ro/TeslaCam")

        # And must have logged a warning.
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and 'cache refresh failed' in r.message.lower()
        ]
        assert len(warnings) == 1, (
            f"Expected exactly 1 warning log on subprocess failure, "
            f"got {len(warnings)}. caplog: "
            f"{[r.message for r in caplog.records]}"
        )

    def test_subprocess_timeout_is_non_fatal(self, _force_present_mode):
        """``TimeoutExpired`` is also non-fatal (legacy contract)."""
        from services.mapping_service import _refresh_ro_mount

        with patch('services.mapping_service.subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["sudo", "tee", "/proc/sys/vm/drop_caches"], timeout=5,
            )
            # Must NOT raise.
            _refresh_ro_mount("/mnt/gadget/part1-ro/TeslaCam")

    def test_source_does_not_contain_legacy_umount_pattern(self):
        """Source-level tripwire: the legacy umount/remount fragments
        must not reappear in ``_refresh_ro_mount``."""
        import inspect
        from services.mapping_service import _refresh_ro_mount

        src = inspect.getsource(_refresh_ro_mount)
        # Forbidden fragments — these were the umount+mount block.
        for forbidden in (
            '"umount"',
            "'umount'",
            'mount", "-o", "ro"',
            "findmnt",
        ):
            assert forbidden not in src, (
                f"Legacy umount/remount fragment '{forbidden}' "
                f"resurfaced in _refresh_ro_mount. Issue #127 "
                f"forbids this pattern."
            )
        # Required fragments.
        assert "/proc/sys/vm/drop_caches" in src, (
            "drop_caches=2 invalidation pattern missing"
        )
        assert '"2\\n"' in src or "'2\\n'" in src or '"2\\n",' in src, (
            "drop_caches input value '2\\n' missing"
        )
