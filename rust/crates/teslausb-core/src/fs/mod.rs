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
//!   `fat32::boot_sector::synthesize` (Phase 2.2),
//!   `fat32::fsinfo::synthesize` (Phase 2.3),
//!   `fat32::fat_table::FatTable` (Phase 2.4),
//!   `fat32::directory` (Phase 2.5), and
//!   `fat32::synth::Fat32Synth` — the byte-offset read dispatcher
//!   that wires all the above together (Phase 2.6). Phase 2.7
//!   added the public-API-only external integration test under
//!   `tests/fs_fat32_integration.rs`.
//! * [`exfat`] — Microsoft `exFAT` implementation. Ships
//!   `exfat::geometry::ExfatGeometry` and
//!   `exfat::boot_sector::synthesize` (Phase 2.8); subsequent
//!   `exfat::*` submodules land in Phases 2.9 – 2.12.
//!
//! ## Planned additions
//!
//! * Phase 2.9 — `exfat::allocation_bitmap` + `exfat::upcase_table`.
//! * Phase 2.10 — `exfat::directory` (file / stream / file-name
//!   entry encoders).
//! * Phase 2.11 — `exfat::synth::ExfatSynth` dispatcher.
//! * Phase 2.12 — `tests/fs_exfat_integration.rs`.

pub mod exfat;
pub mod fat32;
pub mod geometry;
