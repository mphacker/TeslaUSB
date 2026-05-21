# Vendored Third-Party Components in `web/teslausb_web/static/vendor/`

This directory contains third-party JavaScript / CSS libraries bundled
with TeslaUSB B-1. We vendor them locally (rather than loading from a
CDN) because the device frequently runs in **access-point mode** with
no internet — external CDN fetches would simply fail and break the UI.

## Vendored components

| Library              | Version                        | Upstream                                              | License                               | License file                     |
|----------------------|--------------------------------|-------------------------------------------------------|---------------------------------------|----------------------------------|
| Leaflet              | 1.9.4                          | https://leafletjs.com/                                | BSD-2-Clause                          | `leaflet/LICENSE`                |
| Leaflet.markercluster| 1.5.x                          | https://github.com/Leaflet/Leaflet.markercluster      | MIT                                   | `leaflet-markercluster/LICENSE`  |
| protobuf.js          | 7.2.6                          | https://github.com/protobufjs/protobuf.js             | BSD-3-Clause                          | `protobuf/LICENSE`               |
| dashcam-mp4          | Tesla dashcam snapshot         | https://github.com/teslamotors/dashcam                | Upstream license not published        | `dashcam-mp4/LICENSE`            |
| Chart.js             | 4.x                            | https://www.chartjs.org/                              | MIT                                   | (bundled UMD header)             |
| lamejs               | 1.2.1                          | https://github.com/zhuker/lamejs                      | LGPL-3.0                              | `lamejs/LICENSE`                 |

## dashcam-mp4 — usage notes

- `dashcam-mp4` is a Tesla-authored MP4 / SEI parser used by the B-1
  mapping inspector for browser-side telemetry overlays. The vendored
  directory also includes the upstream `dashcam.proto` schema because
  `DashcamHelpers.initProtobuf()` loads it at runtime.
- As of this port, the upstream `teslamotors/dashcam` repository does
  **not** publish a canonical license file or GitHub license metadata.
  We preserved the file to maintain mapping parity with v1 (which
  already shipped the same snapshot in `main`), and recorded the
  unresolved upstream-license gap in `dashcam-mp4/LICENSE` for follow-up.

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
