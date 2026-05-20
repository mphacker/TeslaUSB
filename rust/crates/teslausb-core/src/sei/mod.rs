//! `sei` — Supplemental Enhancement Information extraction
//! from Tesla dashcam MP4 files (Phase 4b.1).
#![allow(clippy::doc_markdown)] // we don't backtick every domain term ("MP4", "BMFF", "GPS")
//!
//! Tesla dashcam clips embed per-frame GPS waypoints, vehicle
//! speed, accelerometer readings, gear state, and autopilot mode
//! as protobuf payloads inside H.264 SEI NAL units. The clip
//! container is MP4 / QuickTime BMFF; the elementary stream
//! inside `mdat` is **AVCC-formatted** (4-byte big-endian length
//! prefixes between NAL units, NOT Annex-B start codes); the SEI
//! payload itself has a Tesla-specific framing on top of the
//! standard ITU-T H.264 SEI envelope.
//! ## Layering (built bottom-up across 4b.1a → 4b.1c)
//!
//! ```text
//!   tesla            ← Tesla protobuf demarshal + SeiMessage   (4b.1c)
//!       ↑
//!   payload          ← H.264 SEI envelope + Tesla padding/marker (4b.1b)
//!       ↑
//!   nal              ← AVCC NAL iterator + emulation-prevention (4b.1a, this slice)
//!       ↑
//!   mp4              ← BMFF box scanner + mvhd extractor        (4b.1a, this slice)
//! ```
//!
//! Each layer is byte-in / structured-data-out, no I/O. The
//! `teslausb-worker::sei` adapter (Phase 4b.1 wrap-up) drives
//! these against `mmap`-ed MP4 files so the worker's RSS stays
//! bounded regardless of clip size.
//!
//! ## Charter compliance
//!
//! Pure logic, no `tokio`, no `std::fs`, no syscalls. Lives in
//! `teslausb-core` per the layering rule. Every public API has
//! a doc comment; every byte-level invariant — header layout,
//! version-1-vs-version-0 differences, integer overflow guards
//! against malicious BMFF — is pinned by tests.
//!
//! ## Why a port from scratch instead of FFI
//!
//! Per `docs/00-PLAN.md` Decision #19, the Rust rewrite is
//! warranted by a measured 5-10× speedup over Python and a
//! 10× reduction in resident memory. Both come from cutting the
//! protobuf-runtime + Python-object overhead per yielded
//! waypoint, which dominates the indexer's CPU budget on Pi
//! Zero 2 W.
//!
//! ## v1 parity
//!
//! The port targets byte-identical agreement with v1's
//! `scripts/web/services/sei_parser.py` for every documented
//! invariant (mvhd creation_time bounds, NAL-type filter set,
//! emulation-prevention placement, Tesla 0x42-padding +
//! 0x69-marker framing). Phase 4b.1c will add a golden-fixture
//! test set against v1's `tests/test_sei_parser.py` once the
//! protobuf layer lands.

pub mod mp4;
pub mod nal;
pub mod payload;
pub mod tesla;
