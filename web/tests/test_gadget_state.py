"""Tests for teslausb_web.services.gadget_state.

Probe behavior matrix:

| UDC file       | lun.0 backing | lun.1 backing | Expected token |
|----------------|---------------|---------------|----------------|
| has content    | has content   | has content   | present        |
| empty / missing| (any)         | (any)         | unknown        |
| has content    | empty/missing | (any)         | unknown        |
| has content    | has content   | empty/missing | unknown        |

We exercise the matrix by pointing the probe at a tmp_path so the
test runs cleanly on any host (no /sys access required).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from teslausb_web.services.gadget_state import gadget_mode_token


def _scaffold(
    root: Path,
    *,
    udc: str | None = "3f980000.usb",
    lun_backings: dict[int, str | None] | None = None,
) -> Path:
    """Build a fake configfs g1 tree under ``root`` and return it."""
    if lun_backings is None:
        lun_backings = {0: "/dev/nbd0", 1: "/dev/nbd1"}
    root.mkdir(parents=True, exist_ok=True)
    if udc is not None:
        (root / "UDC").write_text(udc + "\n")
    for n, backing in lun_backings.items():
        lun_dir = root / "functions" / "mass_storage.usb0" / f"lun.{n}"
        lun_dir.mkdir(parents=True, exist_ok=True)
        if backing is not None:
            (lun_dir / "file").write_text(backing + "\n")
    return root


def test_present_when_udc_bound_and_both_luns_backed(tmp_path: Path) -> None:
    root = _scaffold(tmp_path / "g1")
    assert gadget_mode_token(root) == "present"


def test_unknown_when_udc_missing(tmp_path: Path) -> None:
    root = _scaffold(tmp_path / "g1", udc=None)
    assert gadget_mode_token(root) == "unknown"


def test_unknown_when_udc_empty(tmp_path: Path) -> None:
    root = _scaffold(tmp_path / "g1", udc="")
    assert gadget_mode_token(root) == "unknown"


@pytest.mark.parametrize("missing_lun", [0, 1])
def test_unknown_when_a_lun_has_no_backing_file(
    tmp_path: Path, missing_lun: int
) -> None:
    root = _scaffold(
        tmp_path / "g1",
        lun_backings={0: "/dev/nbd0", 1: "/dev/nbd1", missing_lun: None},
    )
    assert gadget_mode_token(root) == "unknown"


@pytest.mark.parametrize("empty_lun", [0, 1])
def test_unknown_when_a_lun_backing_is_empty(
    tmp_path: Path, empty_lun: int
) -> None:
    root = _scaffold(
        tmp_path / "g1",
        lun_backings={0: "/dev/nbd0", 1: "/dev/nbd1", empty_lun: ""},
    )
    assert gadget_mode_token(root) == "unknown"


def test_unknown_when_configfs_absent(tmp_path: Path) -> None:
    # Pointing at a non-existent path mirrors the case where the
    # gadget module hasn't been loaded yet (early boot or after
    # `modprobe -r dwc2`).
    assert gadget_mode_token(tmp_path / "does-not-exist") == "unknown"
