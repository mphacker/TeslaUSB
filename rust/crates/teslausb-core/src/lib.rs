//! `teslausb-core` — shared domain types for the `TeslaUSB` B-1 reset.
//!
//! This crate is the shared domain core for the full-Rust service
//! layer (`scannerd`, `indexd`, and the rest). Pure logic only; no
//! `tokio`, no `std::fs`, no syscalls. Trivially unit-testable
//! without I/O. Engineering standards are pinned by `SPEC.md` §7.
//!
//! ## Current contents
//!
//! * [`fs`] — filesystem geometry trait + the raw exFAT read/parse
//!   path (boot sector, directory entries, MBR). Consumed by the
//!   `scannerd` raw reader.
//! * [`sei`] — Tesla SEI extraction: MP4 box scanner plus AVCC NAL
//!   iterator plus H.264 emulation-prevention strip; SEI payload
//!   framing + Tesla protobuf demarshal.

pub mod chime;
pub mod fs;
pub mod sei;
