"""Event-zip generator backed by a disk-side tempfile.

Strategy: write the zip to a tempfile in ``cache_dir`` (defaults to
``backing_root/.cache/zip_temp``), then stream the file body to the
HTTP response and unlink the tempfile after the response is sent.

Why a tempfile instead of pure in-memory streaming:

* ``zipfile.ZipFile`` seeks back into the local-file header to
  fix the CRC and size fields after each member. Streaming that
  through a generator is possible but fragile (relies on
  internal-detail knowledge of which bytes are revisited).
* Tesla event folders can hold up to six 1-minute mp4s ≈ 300MB
  total. Holding that in process memory would push gunicorn worker
  RSS into uncomfortable territory.
* The on-disk tempfile lives under ``backing_root`` (NVMe), not
  ``/tmp`` (tmpfs / RAM) — matches v1's GADGET_DIR pattern so the
  Pi's tmpfs cannot run out mid-zip.

There is no Flask import in this module.
"""

from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

_DEFAULT_CHUNK_SIZE: Final[int] = 256 * 1024


def build_event_zip(
    files: tuple[tuple[Path, str], ...],
    cache_dir: Path,
) -> Path:
    """Write a STORED ZIP of ``files`` into ``cache_dir`` and return its path.

    Caller is responsible for unlinking the returned tempfile after
    the HTTP response has been flushed (typically via Flask's
    ``after_this_request`` hook).

    ``ZIP_STORED`` (no compression): mp4 is already H.264-compressed,
    re-compressing burns CPU for ~0% size reduction.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(suffix=".zip", dir=cache_dir)
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for source, arcname in files:
                _add_one(zf, source, arcname)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def _add_one(zf: zipfile.ZipFile, source: Path, arcname: str) -> None:
    """Write one file to ``zf`` in chunks so peak RSS stays bounded.

    ``ZipFile.open(zinfo, "w")`` exposes a writer object; we feed it
    one read-buffer at a time so even a multi-hundred-MB mp4 never
    sits in process memory all at once.
    """
    zinfo = zipfile.ZipInfo(filename=arcname)
    zinfo.compress_type = zipfile.ZIP_STORED
    try:
        with source.open("rb") as src, zf.open(zinfo, "w") as dst:
            while True:
                chunk = src.read(_DEFAULT_CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
    except OSError as exc:
        logger.warning("event_zip: skipping %s: %s", source, exc)
