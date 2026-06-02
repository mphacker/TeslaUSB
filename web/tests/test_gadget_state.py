"""Tests for teslausb_web.services.gadget_state.

Probe behavior matrix (single LUN since the ADR-0023 cutover):

| UDC file       | lun.0 backing | Expected token |
|----------------|---------------|----------------|
| has content    | has content   | present        |
| empty / missing| (any)         | unknown        |
| has content    | empty/missing | unknown        |

We exercise the matrix by pointing the probe at a tmp_path so the
test runs cleanly on any host (no /sys access required).
"""

from __future__ import annotations

from pathlib import Path

from teslausb_web.services.gadget_state import gadget_mode_token


def _scaffold(
    root: Path,
    *,
    udc: str | None = "3f980000.usb",
    lun_backings: dict[int, str | None] | None = None,
) -> Path:
    """Build a fake configfs g1 tree under ``root`` and return it."""
    if lun_backings is None:
        lun_backings = {0: "/dev/nbd0"}
    root.mkdir(parents=True, exist_ok=True)
    if udc is not None:
        (root / "UDC").write_text(udc + "\n")
    for n, backing in lun_backings.items():
        lun_dir = root / "functions" / "mass_storage.usb0" / f"lun.{n}"
        lun_dir.mkdir(parents=True, exist_ok=True)
        if backing is not None:
            (lun_dir / "file").write_text(backing + "\n")
    return root


def test_present_when_udc_bound_and_lun_backed(tmp_path: Path) -> None:
    root = _scaffold(tmp_path / "g1")
    assert gadget_mode_token(root) == "present"


def test_unknown_when_udc_missing(tmp_path: Path) -> None:
    root = _scaffold(tmp_path / "g1", udc=None)
    assert gadget_mode_token(root) == "unknown"


def test_unknown_when_udc_empty(tmp_path: Path) -> None:
    root = _scaffold(tmp_path / "g1", udc="")
    assert gadget_mode_token(root) == "unknown"


def test_unknown_when_lun_has_no_backing_file(tmp_path: Path) -> None:
    root = _scaffold(tmp_path / "g1", lun_backings={0: None})
    assert gadget_mode_token(root) == "unknown"


def test_unknown_when_lun_backing_is_empty(tmp_path: Path) -> None:
    root = _scaffold(tmp_path / "g1", lun_backings={0: ""})
    assert gadget_mode_token(root) == "unknown"


def test_unknown_when_configfs_absent(tmp_path: Path) -> None:
    # Pointing at a non-existent path mirrors the case where the
    # gadget module hasn't been loaded yet (early boot or after
    # `modprobe -r dwc2`).
    assert gadget_mode_token(tmp_path / "does-not-exist") == "unknown"
