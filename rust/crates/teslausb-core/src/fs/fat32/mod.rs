//! Microsoft FAT32 synthesis: layout, boot sector, FAT table,
//! directory entries, and the read dispatcher.
//!
//! Each public module here implements one piece of the FAT32
//! on-disk format. The expectation is that a future
//! `fat32::synth::read` dispatcher (Phase 2.6) consults
//! [`geometry::Fat32Geometry`]'s
//! [`crate::fs::geometry::Geometry::region_at`] method to route an
//! incoming byte-offset to the correct synthesizer module.
//!
//! Phase 2.1 shipped [`geometry`]; Phase 2.2 added
//! [`boot_sector::synthesize`]. Subsequent increments fill in the
//! remaining modules listed in `docs/00-PLAN.md` §Phase 2.
//!
//! ## Spec reference
//!
//! All format decisions cite *Microsoft Extensible Firmware
//! Initiative FAT32 File System Specification* (a.k.a. **fatgen103**),
//! version 1.03, December 6, 2000. Sections are quoted inline.

pub mod boot_sector;
pub mod geometry;
