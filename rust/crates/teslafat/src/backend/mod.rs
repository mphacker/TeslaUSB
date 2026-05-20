//! [`BlockBackend`] implementations local to the `teslafat`
//! daemon.
//!
//! ## Module layout
//!
//! * [`zero`] — `ZeroBackend`: sparse, zero-allocation backend
//!   used by the Phase 1.7 smoke test and any future debug path.
//!   Reads return zeros, writes are no-ops. Holds zero state and
//!   allocates zero bytes.
//! * [`synth`] — `SynthBackend`: Phase 2.19 production backend.
//!   Walks the operator-supplied `backing_root`, plans either a
//!   FAT32 or `exFAT` layout against the configured volume size,
//!   and serves byte-level reads by composing the in-memory
//!   metadata synthesizers with on-demand backing-file reads.
//! * [`dir_tree`] — `DirTreeWriter`: Phase 3.3 POSIX writer
//!   adapter. Routes decoded write chunks (from
//!   [`teslausb_core::fs::fat32::parse::DecodedWrite`] /
//!   [`teslausb_core::fs::exfat::parse::DecodedWrite`]) onto the
//!   backing tree with `.partial`-suffix atomicity. Phase 3.5
//!   will wire it into `SynthBackend::write`.
//!
//! [`BlockBackend`]: teslausb_core::backend::BlockBackend

pub mod dir_tree;
pub mod synth;
pub mod zero;

pub use dir_tree::{DirTreeError, DirTreeWriter, PARTIAL_SUFFIX};
pub use synth::{SynthBackend, SynthBackendError};
pub use zero::ZeroBackend;
