"""Tests for static assets required by the B-1 web UI.

These tests verify that critical assets referenced by B-1 templates
and JavaScript are present, non-empty, and have the expected file
formats for browser/runtime use.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import teslausb_web

PACKAGE_DIR: Path = Path(teslausb_web.__file__).parent
STATIC_DIR: Path = PACKAGE_DIR / "static"

# Files that MUST exist for the B-1 UI paths used by templates and JavaScript.
REQUIRED_FILES: tuple[str, ...] = (
    "tile-cache-sw.js",
    "vendor/dashcam-mp4/dashcam.proto",
    "fonts/inter-variable.woff2",
    "icons/lucide-sprite.svg",
    "css/analytics.css",
    "css/audio_trimmer.css",
    "css/bootstrap-icons-subset.css",
    "css/style.css",
    "js/audio_trimmer.js",
    "js/main.js",
    "js/music.js",
    "vendor/dashcam-mp4/dashcam-mp4.js",
    "vendor/protobuf/protobuf.min.js",
    "vendor/chartjs/chart.umd.min.js",
    "vendor/leaflet/MarkerCluster.css",
    "vendor/leaflet/MarkerCluster.Default.css",
    "vendor/leaflet/leaflet.css",
    "vendor/leaflet/leaflet.js",
    "vendor/leaflet/leaflet.markercluster.js",
    "vendor/leaflet/images/marker-icon-2x.png",
    "vendor/leaflet/images/marker-icon.png",
    "vendor/leaflet/images/marker-shadow.png",
)


@pytest.mark.parametrize("relpath", REQUIRED_FILES)
def test_required_static_file_exists(relpath: str) -> None:
    path = STATIC_DIR / relpath
    assert path.exists(), f"static asset missing from B-1 port: {relpath}"
    assert path.is_file(), f"static asset is not a regular file: {relpath}"
    assert path.stat().st_size > 0, f"static asset is empty: {relpath}"


def test_tile_cache_sw_is_javascript() -> None:
    # The service worker MUST be plain JavaScript (no `module` type)
    # because the registration in main.js uses default options. A
    # syntactic sanity check: the file is non-empty UTF-8 and doesn't
    # start with HTML (which would indicate a bad fetch).
    sw_path = STATIC_DIR / "tile-cache-sw.js"
    head = sw_path.read_bytes()[:200]
    assert not head.lstrip().startswith(b"<"), "tile-cache-sw.js looks like HTML"
    text = head.decode("utf-8", errors="strict")
    assert text  # non-empty, UTF-8 decodable


def test_lucide_sprite_is_svg() -> None:
    sprite = STATIC_DIR / "icons" / "lucide-sprite.svg"
    head = sprite.read_bytes()[:100]
    # SVG either opens with `<?xml` or `<svg`.
    assert head.lstrip().startswith((b"<?xml", b"<svg")), (
        "lucide-sprite.svg is not a valid SVG file"
    )


def test_inter_variable_is_woff2() -> None:
    # WOFF2 files begin with the signature 0x774F4632 ("wOF2").
    woff = STATIC_DIR / "fonts" / "inter-variable.woff2"
    magic = woff.read_bytes()[:4]
    assert magic == b"wOF2", f"inter-variable.woff2 has wrong magic: {magic!r}"


def test_marker_icons_are_png() -> None:
    # PNG magic: 89 50 4E 47 0D 0A 1A 0A.
    png_magic = b"\x89PNG\r\n\x1a\n"
    for name in ("marker-icon.png", "marker-icon-2x.png", "marker-shadow.png"):
        path = STATIC_DIR / "vendor" / "leaflet" / "images" / name
        assert path.read_bytes()[:8] == png_magic, f"{name} has wrong PNG magic"


def test_protobuf_descriptor_is_protobuf_source() -> None:
    # The dashcam.proto descriptor is the protobuf source schema for
    # the embedded SEI metadata. It must declare a syntax line.
    proto = STATIC_DIR / "vendor" / "dashcam-mp4" / "dashcam.proto"
    text = proto.read_text(encoding="utf-8")
    assert "syntax" in text, "dashcam.proto missing syntax declaration"


def test_flask_static_url_resolves_to_real_file() -> None:
    """Flask app served via ``create_app`` must reach the static folder."""
    from teslausb_web.app import create_app
    from teslausb_web.config import FeaturesSection, PathsSection, WebConfig, WebSection

    cfg = WebConfig(
        web=WebSection(secret_key="t" * 32),
        paths=PathsSection(),
        features=FeaturesSection(),
    )
    app = create_app(cfg)
    client = app.test_client()
    # tile-cache-sw is served via the explicit route in app.py, not
    # /static/, but it reads from the same folder.
    resp = client.get("/tile-cache-sw.js")
    assert resp.status_code == 200
    assert resp.mimetype == "application/javascript"
    assert len(resp.data) > 100  # not an empty stub

    # Inter font via Flask's default /static/ handler.
    resp_font = client.get("/static/fonts/inter-variable.woff2")
    assert resp_font.status_code == 200
    assert resp_font.data[:4] == b"wOF2"
