//! Microsoft `exFAT` decoding: layout geometry, boot region,
//! upcase table, and directory entries for the raw reader.
//!
//! Each public module here decodes one piece of the `exFAT`
//! on-disk format consumed by the raw read/parse path
//! ([`parse`]). [`geometry::ExfatGeometry`] maps a byte-offset to a
//! [`crate::fs::geometry::Region`]; [`boot_sector`], [`directory`]
//! and [`dir_decode`] interpret the bytes in those regions;
//! [`upcase_table`] backs case-insensitive name matching.
//!
//! ## Spec reference
//!
//! All format decisions cite *Microsoft `exFAT` File System
//! Specification*, version 1.00, August 27, 2019. Sections are
//! quoted inline. The 12-sector main + 12-sector backup boot
//! regions are §3.1–3.2; the FAT layout is §4; the cluster heap
//! is §5; directory entries are §6.

pub mod boot_sector;
pub mod dir_decode;
pub mod directory;
pub mod geometry;
pub mod parse;
pub mod upcase_table;
