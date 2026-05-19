//! NBD (Network Block Device) protocol bits.
//!
//! Phase 1.3 lands the newstyle handshake in [`handshake`]. The
//! per-connection transmission loop ([`transmission`]) lands in
//! Phase 1.5, dispatching `READ` / `WRITE` / `FLUSH` / `TRIM`
//! against a [`teslausb_core::backend::BlockBackend`] using wire
//! constants and helpers in [`wire`].
//!
//! # Phase 1.6 follow-up
//!
//! TODO(phase-1.6): wrap the [`handshake::run`] call in a
//! `tokio::time::timeout` (30 s is a reasonable starting point) so
//! a misbehaving client that opens a connection and then sends no
//! bytes cannot tie up a slot on the single-threaded runtime. The
//! threat model in Phase 1.3 is low (Unix socket, kernel-only
//! peer) so this is defence-in-depth, not a regression.

pub mod handshake;
pub mod transmission;
pub mod wire;
