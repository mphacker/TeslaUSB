//! Filesystem synthesis primitives for the synthesized `exFAT`
//! volume.
//!
//! Phase 2 of the B-1 rewrite turns the [`crate::backend::BlockBackend`]
//! surface into something the kernel actually recognises as an
//! `exFAT` volume. The on-disk layout is computed lazily by the
//! `exfat::synth` module; the *region map* — i.e. "what kind of
//! bytes live at offset `o`" — is computed eagerly from the volume
//! size by the [`geometry::Geometry`] trait that the read-dispatcher
//! consults on every read.
//!
//! ## Current contents
//!
//! * [`geometry`] — `Geometry` trait, `Region`, `RegionKind`,
//!   `GeometryError`. Pure data; no I/O.
//! * [`exfat`] — Microsoft `exFAT` implementation. Ships
//!   `exfat::geometry::ExfatGeometry`, `exfat::boot_sector`, the
//!   allocation bitmap / up-case table, the directory-entry
//!   synthesizer, and `exfat::synth::ExfatSynth` — the byte-offset
//!   read dispatcher that wires it all together.
//!
//! * [`backing_tree`] — In-memory representation of a real Linux
//!   directory tree plus the shared name-validation rule
//!   (`validate_name`). Filesystem-agnostic; consumed by the
//!   cluster-layout planner and the exFAT dir-entry synthesizer.
//!   The walker that fills a `BackingTree` from `std::fs` lives in
//!   `teslafat::backing_walker`.
//! * [`cluster_layout`] — `ClusterAllocator` that hands out
//!   contiguous cluster ranges plus the `Allocation` value-type.
//!   FS-agnostic; the exFAT layout drives the allocator with
//!   exFAT-specific dir-entry sizing.
//! * `civil_date` — UTC calendar decomposition shared by the
//!   exFAT directory-entry timestamp encoder.

pub mod backing_tree;
pub(crate) mod civil_date;
pub mod cluster_layout;
pub mod cluster_map;
pub mod data_cluster_source;
pub mod exfat;
pub mod geometry;
pub mod mbr;
