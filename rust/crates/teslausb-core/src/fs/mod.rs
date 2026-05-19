//! Filesystem synthesis primitives shared by FAT32 and `exFAT`.
//!
//! Phase 2 of the B-1 rewrite turns the [`crate::backend::BlockBackend`]
//! surface into something the kernel actually recognises as a FAT32
//! or `exFAT` volume. The on-disk layout is computed lazily by the
//! `synth` modules under each FS family; the *region map* — i.e.
//! "what kind of bytes live at offset `o`" — is computed eagerly
//! from the volume size by the [`geometry::Geometry`] trait that
//! the read-dispatcher (Phase 2.6) will consult on every read.
//!
//! ## Current contents
//!
//! * [`geometry`] — `Geometry` trait, `Region`, `RegionKind`,
//!   `GeometryError`. Pure data; no I/O.
//! * [`fat32`] — Microsoft FAT32 implementation. Ships
//!   `fat32::geometry::Fat32Geometry` (Phase 2.1),
//!   `fat32::boot_sector::synthesize` (Phase 2.2), and
//!   `fat32::fsinfo::synthesize` (Phase 2.3). Later increments
//!   add the FAT table, directory, and dispatcher modules.
//!
//! ## Planned additions
//!
//! * Phase 2.4 — `fat32::fat_table::synthesize`.
//! * Phase 2.5 — `fat32::directory::synthesize` (8.3 + LFN).
//! * Phase 2.6 — `fat32::synth::read` dispatcher.
//! * Phase 2.8+ — `exfat::*` parallel modules.

pub mod fat32;
pub mod geometry;
