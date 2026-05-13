"""Issue #152 — drop_caches writes must use the ``sudo tee`` form.

The codebase had two forms for writing to ``/proc/sys/vm/drop_caches``:

1. The legacy ``['sudo', 'sh', '-c', 'echo N > /proc/sys/vm/drop_caches']``
   form in ``partition_mount_service.py`` (lines 656 and 963 pre-fix).
2. The standard ``['sudo', 'tee', '/proc/sys/vm/drop_caches']`` form
   used in ``mapping_service._refresh_ro_mount`` (added by PR #151) and
   documented in ``copilot-instructions.md``.

The ``sh -c "echo ..."`` form spawns an extra shell process per call and
is marginally less safe if any future caller stops using a hardcoded
literal (shell-quoting becomes load-bearing). PR #152 standardizes on
the ``tee`` form everywhere.

These tests pin the new contract for ``partition_mount_service.py``:

1. NO ``sh -c "echo ... > /proc/sys/vm/drop_caches"`` fragments remain.
2. The ``sudo tee`` form IS present (twice — once for the part2
   quick-edit cleanup, once for the part3/music quick-edit cleanup).
3. The input bytes are still ``"3\\n"`` (page + slab caches — both are
   needed in the quick-edit RW transition path, where Tesla just wrote
   to the device and we need to flush BOTH the page and dentry caches
   before remounting RO).
"""
from __future__ import annotations

import inspect


class TestDropCachesPattern:

    def test_no_legacy_sh_c_echo_in_partition_mount_service(self):
        """The two ``sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'``
        call sites in ``partition_mount_service.py`` must be replaced
        with the ``tee`` form. Issue #152."""
        import services.partition_mount_service as pms

        src = inspect.getsource(pms)

        # Forbidden — the old shell-form. If this ever resurfaces, a
        # caller probably copy-pasted from old git history; redirect
        # them at the standard pattern in copilot-instructions.md.
        forbidden_fragments = (
            "'echo 3 > /proc/sys/vm/drop_caches'",
            '"echo 3 > /proc/sys/vm/drop_caches"',
            "'echo 2 > /proc/sys/vm/drop_caches'",
            '"echo 2 > /proc/sys/vm/drop_caches"',
        )
        for fragment in forbidden_fragments:
            assert fragment not in src, (
                f"Legacy shell-form drop_caches write resurfaced "
                f"in partition_mount_service: {fragment!r}. Issue "
                f"#152 forbids this pattern — use ``['sudo', 'tee', "
                f"'/proc/sys/vm/drop_caches']`` with ``input='3\\n'`` "
                f"(see mapping_service._refresh_ro_mount for the "
                f"reference shape)."
            )

    def test_uses_tee_form_in_partition_mount_service(self):
        """The standard ``sudo tee`` shape must be present at both
        quick-edit cleanup sites (part2 and part3/music)."""
        import services.partition_mount_service as pms

        src = inspect.getsource(pms)

        # The reference shape, exactly as in mapping_service.
        # We expect this to appear at least twice (part2 + part3
        # cleanup paths). Don't anchor to a count; just require ≥ 2.
        tee_call_count = src.count(
            "['sudo', 'tee', '/proc/sys/vm/drop_caches']"
        )
        assert tee_call_count >= 2, (
            f"Expected at least 2 ``sudo tee`` drop_caches calls in "
            f"partition_mount_service (one for part2 quick-edit "
            f"cleanup, one for part3/music quick-edit cleanup); "
            f"found {tee_call_count}. Issue #152."
        )

        # Both call sites must still write "3\n" (page + slab) — the
        # quick-edit RW transition path needs to flush BOTH caches
        # before remounting RO. Don't accidentally weaken to "2\n"
        # (slabs only, which would leave stale page-cache pages).
        assert "input='3\\n'" in src or 'input="3\\n"' in src, (
            "drop_caches input must still be '3\\n' in "
            "partition_mount_service (page + slab caches). Issue "
            "#152 changed only the subprocess SHAPE, not the byte."
        )
