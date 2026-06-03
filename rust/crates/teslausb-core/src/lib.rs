//! `teslausb-core` — shared domain types for the `TeslaUSB` B-1 daemon.
//!
//! This crate is the dependency-inversion seam between the
//! `teslafat` binary (NBD + FS synthesis adapters) and the
//! `teslausb-worker` binary (retention/cloud-sync worker). Both
//! depend on this crate; nothing here depends on either.
//!
//! Per `docs/03-CODE-QUALITY-CHARTER.md` §"Best Architecture
//! Practices" — domain core. Pure logic only; no `tokio`, no
//! `std::fs`, no syscalls. Trivially unit-testable without I/O.
//!
//! ## Current contents
//!
//! * [`ipc`] — versioned envelope + `STATUS` / `RETENTION_UPDATE` /
//!   `INVALIDATE_CACHE` request/response types (Phase 1.2).
//! * [`backend`] — `BlockBackend` trait + `WriteFlags` + reference
//!   `NullBackend` / `MockBackend` impls (Phase 1.4). Used by the
//!   `teslafat` NBD transmission loop in Phase 1.5+ and by the
//!   real file-backed backend that follows.
//! * [`fs`] — filesystem geometry trait + exFAT implementation.
//!   Consumed by `teslafat`'s read-dispatcher.
//! * [`sei`] — Tesla SEI extraction (Phase 4b.1): MP4 box scanner
//!   plus AVCC NAL iterator plus H.264 emulation-prevention strip;
//!   SEI payload framing + Tesla protobuf demarshal land in 4b.1b/c.

pub mod backend;
pub mod fs;
pub mod ipc;
pub mod sei;
