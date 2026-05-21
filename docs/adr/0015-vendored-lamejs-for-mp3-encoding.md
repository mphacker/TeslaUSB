# ADR-0015: Vendor lamejs locally for MP3 encoding in the web UI

**Status:** Accepted
**Date:** 2026-05-21
**Phase:** 5.8e (lock_chimes UI port)
**Supersedes:** none
**Related:** docs/05-UI-UX-DESIGN-SYSTEM.md §"Performance & Bundle Size"

## Context

The v1 web UI used [lamejs 1.2.1](https://github.com/zhuker/lamejs)
(a pure-JavaScript port of LAME) for browser-side MP3 re-encoding in
the lock-chime audio trimmer. v1 loaded it from a public CDN:

    <script src="https://cdn.jsdelivr.net/npm/lamejs@1.2.1/lame.min.js">

Two B-1 constraints block this approach:

1. **Offline operation.** TeslaUSB devices spend most of their lives
   on an isolated AP network with no internet route. Any CDN load
   would fail and break the audio-trimmer UI.
2. **UI Design System §"Performance & Bundle Size"** explicitly
   forbids external resource loading: *"No external CDN calls"*.

We must therefore either vendor lamejs locally, or remove the
MP3 re-encoding feature.

## Decision

**Vendor `lame.min.js` (lamejs 1.2.1) under
`web/teslausb_web/static/vendor/lamejs/` and load it locally via
`url_for('static', filename='vendor/lamejs/lame.min.js')`.**

Bundle structure:
- `static/vendor/lamejs/lame.min.js` — minified library, ~156 KB
- `static/vendor/lamejs/LICENSE` — full LGPL-3.0 text (mandatory)
- `static/vendor/README.md` — vendor manifest + update procedure

The vendored file's first line contains an attribution comment
identifying the version, the date vendored, the upstream URL, and
the license.

## Consequences — Positive

- Audio trimmer works offline on the device (the primary use case).
- No flaky CDN dependency in a production runtime.
- Predictable bundle size (no surprise CDN bundle changes).
- Faster page load — no DNS lookup, no external TLS handshake.

## Consequences — Negative / Obligations

- **LGPL-3.0 compliance.** lamejs is LGPL-3.0 because it derives
  from LAME (the C reference encoder). The license allows linking
  from a non-LGPL application (TeslaUSB is itself open-source)
  provided we:
  - Distribute the unmodified `lame.min.js` alongside the LGPL
    license text (done — `static/vendor/lamejs/LICENSE`).
  - Keep the upstream attribution comment in the file header
    (done).
  - Permit users to relink the application with a modified
    `lame.min.js` (trivially possible: it's served as a separate
    `<script src=...>` so anyone can substitute their own build).
  - If we modify `lame.min.js` itself, release those changes under
    LGPL-3.0. **We have NOT modified the upstream file.** Any
    future modification must update this ADR and contribute the
    modified file back upstream.

- **156 KB bundle increase.** lamejs is shipped to every browser
  visiting `/lock_chimes/`. Mitigations:
  - Gzip transport-encoding reduces this to ~40 KB on the wire.
  - The page that needs it (lock_chimes) is not on the critical
    boot path — health, dashboard, and video playback all load
    without it.
  - Long browser cache via aggressive `Cache-Control: max-age=...`
    in the eventual nginx config (Phase 5.19).

- **Update discipline.** We've pinned to 1.2.1 (the v1 version) to
  preserve byte-identical UI behavior. Future upgrades must
  re-verify the audio-trimmer round-trip + retest WAV/MP3 round
  trip + bump this ADR's "version" field.

## Alternatives considered

1. **Remove MP3 encoding from the audio trimmer.** Rejected — the
   operator's binding constraint is that the B-1 web UI must
   "look, feel, and operate the same as v1 EXCEPT for USB-handling
   differences." MP3 encoding is a user-facing audio feature
   unrelated to USB handling, so removing it would violate UI
   parity.

2. **Replace lamejs with `<canvas>`-based MP3 encoding (e.g., using
   the Web Audio API + an Emscripten-built encoder).** Rejected —
   pure rewriting of an audio pipeline for license-only reasons is
   exactly the "shortcut that took the easy way out" the Code
   Quality Charter §3 forbids in reverse: we'd be **adding** work
   to avoid a clearly-acceptable license compliance step.

3. **Use a server-side ffmpeg endpoint** for MP3 encoding instead
   of browser-side. Rejected — would push CPU load onto the
   already-constrained Pi (the device with a single Cortex-A72 +
   limited RAM, also juggling the `teslafat` daemon, the indexer,
   gunicorn, nginx, and Samba). Browser-side encoding is
   architecturally cleaner and shifts load off the device.

## Verification

- The vendored `lame.min.js` was downloaded from
  `cdn.jsdelivr.net/npm/lamejs@1.2.1/lame.min.js` (the URL v1
  used). File size 156,187 bytes. SHA-256 to be recorded in the
  initial vendor commit for future tamper-evidence.
- The full LGPL-3.0 text was downloaded from
  `https://www.gnu.org/licenses/lgpl-3.0.txt` (7,652 bytes,
  canonical FSF copy).
- A test in `web/tests/test_lock_chimes_blueprint.py` asserts the
  template references `/static/vendor/lamejs/lame.min.js` (NOT
  the CDN URL) — protects against accidental regressions.

## Open questions

None.
