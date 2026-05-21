# Vendored Third-Party Components in `web/teslausb_web/static/vendor/`

This directory contains third-party JavaScript / CSS libraries bundled
with TeslaUSB B-1. We vendor them locally (rather than loading from a
CDN) because the device frequently runs in **access-point mode** with
no internet — external CDN fetches would simply fail and break the UI.

## Vendored components

| Library    | Version | Upstream                                              | License     | License file                |
|------------|---------|-------------------------------------------------------|-------------|-----------------------------|
| Leaflet    | 1.9.x   | https://leafletjs.com/                                | BSD-2-Clause| `leaflet/LICENSE`           |
| Chart.js   | 4.x     | https://www.chartjs.org/                              | MIT         | (bundled UMD header)        |
| lamejs     | 1.2.1   | https://github.com/zhuker/lamejs                      | LGPL-3.0    | `lamejs/LICENSE`            |

## lamejs — usage notes

- `lamejs` (a pure-JS MP3 encoder derived from LAME) is loaded as a
  separate file by `templates/lock_chimes.html` to support browser-
  side MP3 encoding for the audio trimmer (originally a v1 feature
  preserved verbatim in B-1 per the UI parity contract).
- LGPL-3.0 allows linking from a non-LGPL application (TeslaUSB is
  itself open-source under its project license) provided we
  preserve the LGPL license text, do not strip attribution, and
  permit relinking with a modified `lame.min.js`. We satisfy this
  by serving `lame.min.js` as a separate file under
  `static/vendor/lamejs/` with the unmodified file header and an
  adjacent `LICENSE` containing the full LGPL-3.0 text.
- See `docs/adr/0015-vendored-lamejs-for-mp3-encoding.md` for the
  decision record.

## Updating vendored components

Update procedure (for each library above):

1. Download the new release source (GitHub release tarball or
   official CDN URL — record the SHA-256 in the commit message).
2. Replace the file(s) in `static/vendor/<lib>/`.
3. Verify the license file is current.
4. Run the full test suite + a manual smoke test that the
   component still functions.
5. Update the version column in this file.
