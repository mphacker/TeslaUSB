"""USB-gadget state probe.

The B-1 architecture chains four pieces of state to present USB to
the Tesla:

    teslafat@0  →  /run/teslausb/teslafat-0.sock (NBD server)
        ↓ (over NBD)
    nbd-attach@0  →  /dev/nbd0 (NBD client)
        ↓ (block device)
    usb-gadget         →  configfs /sys/kernel/config/usb_gadget/g1
        ↓ (LUN file)
    UDC binding        →  /sys/kernel/config/usb_gadget/g1/UDC

Any link in that chain can break independently. v1's web app had
a binary "mode" state (present / edit) it could read from a
shell script; B-1 has no such single source of truth, so we
probe the actual kernel state at request time and synthesize a
status token the dashboard template understands.

After the ADR-0023 single-LUN cutover the gadget exposes ONE
mass-storage LUN (``lun.0``) backed by ``/dev/nbd0`` — a single
MBR-partitioned disk that Tesla sees as one USB drive with two
exFAT partitions (TeslaCam + media). There is no ``lun.1``.

State tokens returned (matches index.html's ``mode_token`` branches):

* ``"present"``  — gadget bound to a UDC AND the LUN backing file
                   points at a real block device. Tesla sees the
                   USB drive.
* ``"unknown"``  — anything else (UDC empty, LUN missing, configfs
                   absent, sysfs unreadable). Dashboard shows the
                   orange "Status Unknown" card.

This module reads only configfs / sysfs paths that the web app's
unprivileged user (``pi``) can stat. No sudo, no IPC, no service
shell-outs — those would be expensive on every page render.
"""

from __future__ import annotations

from pathlib import Path

# Production configfs root for the gadget defined by setup-lib/04-units.sh.
_GADGET_ROOT = Path("/sys/kernel/config/usb_gadget/g1")
_UDC = _GADGET_ROOT / "UDC"
_LUN_FILE_TEMPLATE = "functions/mass_storage.usb0/lun.{n}/file"
# Single LUN since the ADR-0023 cutover (one partitioned disk on nbd0).
_EXPECTED_LUNS = (0,)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def gadget_mode_token(
    gadget_root: Path = _GADGET_ROOT,
    expected_luns: tuple[int, ...] = _EXPECTED_LUNS,
) -> str:
    """Return the dashboard status token for the live gadget.

    ``gadget_root`` and ``expected_luns`` are injectable to keep
    unit tests filesystem-independent — production callers use
    the module-level defaults.
    """
    udc = _read_text(gadget_root / "UDC")
    if not udc:
        return "unknown"
    for n in expected_luns:
        backing = _read_text(gadget_root / _LUN_FILE_TEMPLATE.format(n=n))
        if not backing:
            return "unknown"
    return "present"


__all__ = ["gadget_mode_token"]
