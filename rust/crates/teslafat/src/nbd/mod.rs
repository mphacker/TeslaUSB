//! NBD (Network Block Device) protocol bits.
//!
//! Phase 1.3 lands the newstyle handshake in [`handshake`]. The
//! per-connection transmission loop (`READ` / `WRITE` / `FLUSH` /
//! `TRIM` dispatch over a `BlockBackend`) is Phase 1.5.

pub mod handshake;
