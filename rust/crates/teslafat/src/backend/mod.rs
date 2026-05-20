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
//!   wires it into `SynthBackend::write` via [`fat32_write`].
//! * [`fat32_write`] — `Fat32WriteState`: Phase 3.5c state machine
//!   that orchestrates `decode_write` → directory-entry decoding
//!   → FAT chain walking → cluster-map insertion → `dir_tree`
//!   routing, with crash-safe finalize on flush.
//! * [`exfat_write`] — `ExfatWriteState`: Phase 3.5e parallel
//!   state machine for `exFAT`. Shares the same crash-safe
//!   `.partial` rename discipline; differs in carrying
//!   `PartialEntrySet` between dir-cluster boundaries and
//!   short-circuiting the FAT walk for the common
//!   `NoFatChain == true` extents.
//!
//! [`BlockBackend`]: teslausb_core::backend::BlockBackend

pub mod dir_tree;
pub mod dirty_map;
pub mod exfat_write;
pub mod fat32_write;
pub mod synth;
pub mod zero;

pub use dir_tree::{DirTreeError, DirTreeWriter, PARTIAL_SUFFIX};
pub use exfat_write::{ExfatWriteError, ExfatWriteState};
pub use fat32_write::{Fat32WriteError, Fat32WriteState};
pub use synth::{SynthBackend, SynthBackendError};
pub use zero::ZeroBackend;
