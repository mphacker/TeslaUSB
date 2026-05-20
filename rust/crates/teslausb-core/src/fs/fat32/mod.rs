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
//! [`boot_sector::synthesize`]; Phase 2.3 added
//! [`fsinfo::synthesize`]; Phase 2.4 added
//! [`fat_table::FatTable`]; Phase 2.5 added
//! [`directory::synthesize_sfn_entry`] +
//! [`directory::synthesize_lfn_sequence`]; Phase 2.6 added
//! [`synth::Fat32Synth`] which dispatches byte-offset reads
//! across all the above. Subsequent increments add directory-tree
//! layout (Phase 2.7+).
//!
//! ## Spec reference
//!
//! All format decisions cite *Microsoft Extensible Firmware
//! Initiative FAT32 File System Specification* (a.k.a. **fatgen103**),
//! version 1.03, December 6, 2000. Sections are quoted inline.

pub mod boot_sector;
pub mod chain;
pub mod dir_decode;
pub mod directory;
pub mod fat_table;
pub mod fsinfo;
pub mod geometry;
pub mod layout;
pub mod parse;
pub mod synth;
