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
//!
//! [`BlockBackend`]: teslausb_core::backend::BlockBackend

pub mod synth;
pub mod zero;

pub use synth::{SynthBackend, SynthBackendError};
pub use zero::ZeroBackend;
