//! `scannerd` — R1 raw exFAT/MP4/SEI reader (`docs/specs/scannerd.md`).
//!
//! Indexes the car's recorded media by reading the **raw** USB
//! mass-storage backing image directly — `MBR → exFAT → FAT chain →
//! MP4 → H.264 SEI` — and **never mounting** the Tesla filesystem, so
//! it can never interfere with the car's writes (the #1 invariant).
//!
//! Emits facts (file identity, timestamps, partition, clip grouping,
//! SEI samples) only for files proven **stable across scans**; anything
//! in flux is skipped and retried. It derives no trips/events — that is
//! `indexd`'s job.
//!
//! ## Layering
//!
//! The parse / traversal / stability-gating logic is **pure** and
//! host-testable: every byte comes through the [`reader::BlockReader`]
//! trait, so tests drive it over an in-memory image. The real `pread`
//! syscalls, the scan loop, and the I/O-priority handling live in the
//! binary (`main.rs`). Low-level exFAT directory-entry decoding and the
//! whole SEI pipeline are reused from `teslausb-core`.
//!
//! ## Modules
//!
//! * [`reader`] — the `BlockReader` abstraction + in-memory test impl.
//! * [`mbr`] — MBR primary partition-table parser (read path).
//! * [`boot`] — exFAT boot-sector / BPB parser + cluster/offset math.
//! * [`error`] — the crate error type.

pub mod boot;
pub mod clip;
pub mod error;
pub mod mbr;
pub mod mp4probe;
pub mod reader;
pub mod seiscan;
pub mod stability;
pub mod volume;
pub mod walk;
