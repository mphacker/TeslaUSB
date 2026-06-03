//! Microsoft `exFAT` synthesis: layout, boot region, allocation
//! bitmap, upcase table, directory entries, and the read dispatcher.
//!
//! Each public module here implements one piece of the `exFAT`
//! on-disk format. The expectation is that a future
//! `exfat::synth::read` dispatcher (Phase 2.11) consults
//! [`geometry::ExfatGeometry`]'s
//! [`crate::fs::geometry::Geometry::region_at`] method to route an
//! incoming byte-offset to the correct synthesizer module.
//!
//! This module ships [`geometry`], [`boot_sector`],
//! [`allocation_bitmap`], [`upcase_table`], [`directory`], and the
//! [`synth`] read dispatcher, exercised by the external
//! `fs_exfat_integration.rs` harness.
//!
//! ## Spec reference
//!
//! All format decisions cite *Microsoft `exFAT` File System
//! Specification*, version 1.00, August 27, 2019. Sections are
//! quoted inline. The 12-sector main + 12-sector backup boot
//! regions are §3.1–3.2; the FAT layout is §4; the cluster heap
//! is §5; directory entries are §6.

pub mod allocation_bitmap;
pub mod boot_sector;
pub mod dir_decode;
pub mod directory;
pub mod geometry;
pub mod layout;
pub mod lazy_load;
pub mod parse;
pub mod synth;
pub mod upcase_table;
