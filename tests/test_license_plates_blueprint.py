"""Blueprint-level tests for license_plates routes.

These cover behaviour that lives in the route layer (not the service):

* Image gating via the ``before_request`` hook.
* Filename / partition validation in ``download_plate``.
* The defense-in-depth ``os.path.commonpath()`` containment check
  added to ``download_plate`` — a symlink under ``LicensePlate/``
  pointing outside the folder must NOT be served.

The wider blueprint imports plenty of optional Pi-runtime modules
(samba, mode-control), so we mount it inside a hermetic Flask app and
patch the image-path / mount-path indirection points used by the
route.
"""
import os
import sys

import pytest
from flask import Flask


def _tmp_image_file(tmp_path, name='usb_lightshow.img'):
    """Create a sentinel file the ``before_request`` hook checks."""
    f = tmp_path / name
    f.write_bytes(b"\x00")
    return str(f)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Build a hermetic Flask app with the license_plates blueprint mounted.

    Patches ``IMG_LIGHTSHOW_PATH`` so the gating hook passes, and
    monkey-patches ``get_mount_path`` (imported into the blueprint
    module) so the route reads from a tmp directory rather than a
    real USB mount.
    """
    from blueprints import license_plates as lp_module

    img_path = _tmp_image_file(tmp_path)
    mount_path = tmp_path / 'mnt' / 'gadget' / 'part2'
    plates_dir = mount_path / 'LicensePlate'
    plates_dir.mkdir(parents=True)

    monkeypatch.setattr(lp_module, 'IMG_LIGHTSHOW_PATH', img_path)
    monkeypatch.setattr(lp_module, 'get_mount_path', lambda part: str(mount_path))

    flask_app = Flask(__name__)
    flask_app.secret_key = 'test'
    flask_app.register_blueprint(lp_module.license_plates_bp)

    # Stub the mode_control blueprint the gating hook redirects to
    # so the 302 response can resolve cleanly.
    from flask import Blueprint
    mode_bp = Blueprint('mode_control', __name__)

    @mode_bp.route('/mode_control/')
    def index():
        return 'index', 200

    flask_app.register_blueprint(mode_bp)

    flask_app.config['TESTING'] = True
    flask_app.plates_dir = plates_dir
    flask_app.mount_path = mount_path
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------
# download_plate path-traversal containment
# ---------------------------------------------------------------
class TestDownloadPlateContainment:
    """The download route MUST verify the resolved path lives under
    the LicensePlate folder before serving it. ``basename()`` defangs
    ``../`` traversal in the filename string itself, but a symlink
    living under LicensePlate/ pointing outside the folder would
    still be served without the ``commonpath()`` check.
    """

    def test_download_normal_file_succeeds(self, app, client):
        """Sanity: a real PNG in the plates dir downloads OK."""
        png = app.plates_dir / 'realplate.png'
        png.write_bytes(b'\x89PNG\r\n\x1a\n')
        r = client.get('/license_plates/download/part2/realplate.png')
        assert r.status_code == 200
        assert r.data.startswith(b'\x89PNG')

    def test_download_traversal_in_filename_blocked_by_routing(self, app, client):
        """Defense-in-depth check #1: werkzeug URL routing rejects
        URL-encoded slashes (``%2F``) in the ``<filename>`` segment
        because the default string converter doesn't allow them.
        Result is a 404 from the routing layer — the request never
        reaches our handler. This is the *first* line of defense
        against traversal; ``basename()`` and ``commonpath()`` are
        the second and third."""
        r = client.get(
            '/license_plates/download/part2/..%2F..%2Fetc%2Fpasswd.png',
            follow_redirects=False,
        )
        # 404 from routing OR 302 from missing-file branch — both
        # mean "did not serve a file outside the plates dir". The
        # critical assertion is that no sensitive data leaks.
        assert r.status_code in (302, 404)

    def test_download_missing_file_redirects(self, app, client):
        """A request for a non-existent .png inside the plates dir
        flashes + redirects (302) — never returns the file path on
        disk or any other identifying error."""
        r = client.get(
            '/license_plates/download/part2/nonexistent.png',
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_download_non_png_extension_blocked(self, app, client):
        """Even an existing file with a non-.png extension must be
        refused — the route hard-codes the .png extension check."""
        bad = app.plates_dir / 'sneaky.txt'
        bad.write_bytes(b'hello')
        r = client.get(
            '/license_plates/download/part2/sneaky.txt',
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_download_invalid_partition_redirects(self, app, client):
        """Partition name not in USB_PARTITIONS → 302."""
        r = client.get(
            '/license_plates/download/bogus/anything.png',
            follow_redirects=False,
        )
        assert r.status_code == 302

    @pytest.mark.skipif(
        sys.platform == 'win32' and not os.environ.get('TESTS_ALLOW_SYMLINK'),
        reason='Symlink creation on Windows requires admin or developer mode; '
               'set TESTS_ALLOW_SYMLINK=1 to run this test there',
    )
    def test_download_symlink_outside_plates_dir_blocked(
            self, app, client, tmp_path):
        """Defense-in-depth: a symlink under LicensePlate/ pointing
        OUTSIDE the folder must NOT be served. This is the case the
        ``os.path.commonpath()`` containment check defends against —
        ``basename()`` alone would let it through.
        """
        # Create a sensitive file outside the plates dir.
        outside = tmp_path / 'secret.png'
        outside.write_bytes(b'\x89PNG\r\n\x1a\nSECRET-DATA')

        # Plant a symlink inside the plates dir pointing at it.
        link = app.plates_dir / 'evil.png'
        try:
            os.symlink(str(outside), str(link))
        except (OSError, NotImplementedError):
            pytest.skip('Symlink creation not permitted on this platform')

        r = client.get(
            '/license_plates/download/part2/evil.png',
            follow_redirects=False,
        )
        # The commonpath check rejects it with a flash + 302 redirect.
        # Crucially, the response must NOT contain the secret payload.
        assert r.status_code == 302
        assert b'SECRET-DATA' not in r.data

    def test_download_realpath_outside_blocked_via_mock(
            self, app, client, monkeypatch):
        """Portable variant of the symlink test that works on Windows
        without admin: monkey-patch ``os.path.realpath`` so the
        resolved path lands outside the plates dir, exercising the
        ``commonpath()`` rejection branch deterministically.
        """
        png = app.plates_dir / 'evil.png'
        png.write_bytes(b'\x89PNG\r\n\x1a\nSECRET-DATA')

        plates_dir_real = os.path.realpath(str(app.plates_dir))
        evil_target = os.path.realpath(
            str(app.mount_path.parent.parent)  # repo-tmp root, definitely outside
        )

        from blueprints import license_plates as lp_module

        original_realpath = os.path.realpath

        def fake_realpath(p):
            # The route resolves both the expected dir and the file.
            # Keep the expected dir honest, but pretend the file
            # resolves to evil_target (outside the plates folder).
            if p.endswith('evil.png'):
                return evil_target
            return original_realpath(p)

        monkeypatch.setattr(lp_module.os.path, 'realpath', fake_realpath)

        r = client.get(
            '/license_plates/download/part2/evil.png',
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert b'SECRET-DATA' not in r.data

    def test_download_no_mount_path_redirects(self, app, client, monkeypatch):
        """If get_mount_path returns falsy (partition not mounted),
        route redirects without touching the filesystem."""
        from blueprints import license_plates as lp_module
        monkeypatch.setattr(lp_module, 'get_mount_path', lambda part: None)
        r = client.get(
            '/license_plates/download/part2/realplate.png',
            follow_redirects=False,
        )
        assert r.status_code == 302


# ---------------------------------------------------------------
# Image gating via before_request
# ---------------------------------------------------------------
class TestImageGating:

    def test_routes_blocked_when_image_missing(self, app, client, monkeypatch):
        from blueprints import license_plates as lp_module
        monkeypatch.setattr(
            lp_module, 'IMG_LIGHTSHOW_PATH', '/nonexistent/path.img')
        r = client.get(
            '/license_plates/download/part2/realplate.png',
            follow_redirects=False,
        )
        # Browser request → flash + redirect (302).
        assert r.status_code == 302

    def test_ajax_request_gets_503_json_when_image_missing(
            self, app, client, monkeypatch):
        from blueprints import license_plates as lp_module
        monkeypatch.setattr(
            lp_module, 'IMG_LIGHTSHOW_PATH', '/nonexistent/path.img')
        r = client.get(
            '/license_plates/download/part2/realplate.png',
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        assert r.status_code == 503
        body = r.get_json()
        assert body == {'error': 'Feature unavailable'}
