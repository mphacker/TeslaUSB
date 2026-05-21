# ADR-0016: Vendored Tesla dashcam-mp4.js for SEI Metadata Interop

**Status:** ACCEPTED (with tracked follow-up)

**Date:** 2025-06-25

**Authors:** B-1 rewrite, Phase 5.13e

**Supersedes:** None

**Superseded by:** None

## Context

Phase 5.13 (mapping page) needs to play Tesla dashcam MP4 clips in the
browser AND overlay their SEI telemetry (speed, gear, autopilot state,
etc.) on the video timeline. SEI is the H.264 user-data NAL type Tesla
embeds in dashcam footage, encoding telemetry as a protobuf message.

Two third-party JS files are needed:

1. **dashcam-mp4.js** — Tesla's own MP4 box parser + SEI extractor.
   Source: <https://github.com/teslamotors/dashcam> (`dashcam-mp4.js`).
2. **dashcam.proto** — Tesla's protobuf schema for the SEI message
   (`SeiMetadata`). Source: same repo (`dashcam.proto`).

## License situation

The `teslamotors/dashcam` GitHub repository (checked 2025-06-25) does
NOT contain a LICENSE file and has no `license` field in its GitHub
metadata. The repo is public; Tesla published it for community
interop with their dashcam format. Issue
<https://github.com/teslamotors/dashcam/issues/6> tracks community
requests for a license but is unresolved.

Strict reading: "no license" means the code is copyrighted by Tesla
with no permission to copy or redistribute. Vendoring under those
terms is legally risky for a public project.

Practical reading: Tesla published this code publicly as the *only*
documented mechanism for interop with the SEI metadata in their own
dashcam files. The repository README explicitly invites community
use ("Use the online SEI Explorer → … Just drag and drop your MP4
file"). There is at minimum an implied permission to use for the
documented purpose (interop with Tesla dashcam clips).

## Decision

Vendor `dashcam-mp4.js` and `dashcam.proto` under
`web/teslausb_web/static/vendor/dashcam-mp4/` for the documented
interop purpose. Distribute alongside a LICENSE note that explicitly
documents the upstream license uncertainty, links to the upstream
repo and tracking issue, and references this ADR.

This is a **pragmatic acceptance** — not a clean license grant. We
explicitly accept the legal uncertainty as appropriate for an
interop tool whose users are Tesla owners using their own clips.

## Mitigation / follow-up

Tracked as a known issue. Two paths forward, in priority order:

1. **Wait for upstream**: monitor `teslamotors/dashcam` for a
   published LICENSE. Once Tesla publishes (most likely BSD-3 or
   MIT, matching other Tesla open-source repos), update the
   LICENSE file in our vendor directory and remove the uncertainty
   note. No code change needed.

2. **Clean-room re-implementation**: if Tesla never publishes a
   license, write our own MP4 box parser + SEI extractor from
   scratch (MP4 box format is publicly documented in ISO/IEC
   14496-12; the SEI extraction is straightforward NAL-unit
   parsing). The .proto schema is itself published by Tesla and
   describes a wire format — protobuf schemas describing wire
   formats are generally not subject to copyright the same way as
   code. Filing a tracking issue: <link TBD when issue opened>.

## Alternatives considered

- **mp4box.js** (BSD-3-Clause, ~200 KB minified). Pros: explicit
  permissive license, battle-tested. Cons: significantly larger;
  doesn't natively understand Tesla's SEI payload format —
  we'd still need our own protobuf decode glue, eliminating most
  of the simplification benefit.

- **Server-side extraction only** (have the Python service decode
  SEI and ship JSON to the browser). Pros: zero JS license
  surface; reuses Phase 4b SEI parser. Cons: the *video frame*
  display still needs MP4 demux in the browser; the SEI-on-server
  approach would require pre-decoding every clip on demand which
  is too slow for Pi Zero 2 W.

- **Remove the SEI overlay feature**. Charter compliant but
  removes v1 parity. Operator directive ("full v1 parity except
  USB-handling") rules this out.

## Consequences

- Phase 5.13 ships with a vendor file whose license is uncertain.
- Distribution is reasonable but not legally airtight.
- A linked GitHub issue tracks the upstream license resolution.
- If a downstream user or auditor objects, we can swap to the
  clean-room implementation at any time without API changes
  (`window.DashcamMP4` is the only surface).
- LICENSE file in `static/vendor/dashcam-mp4/` references this
  ADR explicitly.

## Update procedure (when upstream resolves)

1. Pull the new `LICENSE` text from the upstream repo.
2. Replace `web/teslausb_web/static/vendor/dashcam-mp4/LICENSE`
   with the canonical license text from the official source.
3. Update `web/teslausb_web/static/vendor/README.md` entry to
   reflect the actual license (e.g. BSD-3-Clause).
4. Mark this ADR as superseded by a new ADR (e.g. ADR-0017)
   documenting the resolved license, or amend this ADR's status
   to "RESOLVED" and add a resolution section.
