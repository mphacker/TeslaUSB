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
//! ## Phase 0.2 state
//!
//! Empty crate skeleton — compiles clean, ships nothing. Real
//! contents land in:
//!
//! * Phase 1.2 — `ipc::messages` (versioned envelope, `STATUS` /
//!   `RETENTION_UPDATE` / `INVALIDATE_CACHE`).
//! * Phase 1.4 — `backend::BlockBackend` trait (size / read /
//!   write(flags) / flush, with FUA contract documented).
//! * Phase 2.1+ — `fs::geometry` trait shared by FAT32 and `exFAT`
//!   geometry implementations.
