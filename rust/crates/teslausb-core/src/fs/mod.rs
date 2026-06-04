//! Filesystem primitives for the raw `exFAT` read/parse path.
//!
//! The *region map* — i.e. "what kind of bytes live at offset `o`"
//! — is computed from the volume size by the [`geometry::Geometry`]
//! trait that the raw reader consults on every read. The `exfat`
//! submodule decodes the boot region, directory entries and MBR
//! that the `scannerd` reader relies on.
//!
//! ## Current contents
//!
//! * [`geometry`] — `Geometry` trait, `Region`, `RegionKind`,
//!   `RegionMapError`. Pure data; no I/O.
//! * [`exfat`] — Microsoft `exFAT` decoders: `exfat::geometry`,
//!   `exfat::boot_sector`, the up-case table, and the
//!   directory-entry encode/decode helpers used by the raw reader.
//! * [`mbr`] — Master Boot Record partition-table decoding.
//! * `civil_date` — UTC calendar decomposition shared by the
//!   exFAT directory-entry timestamp encoder.

pub(crate) mod civil_date;
pub mod exfat;
pub mod geometry;
pub mod mbr;
