//! NBD (Network Block Device) protocol bits.
//!
//! Phase 1.3 lands the newstyle handshake in [`handshake`]. The
//! per-connection transmission loop ([`transmission`]) lands in
//! Phase 1.5, dispatching `READ` / `WRITE` / `FLUSH` / `TRIM`
//! against a [`teslausb_core::backend::BlockBackend`] using wire
//! constants and helpers in [`wire`].
//!
//! The handshake-timeout safety net (the inc-1.3 follow-up) lands
//! in Phase 1.6 in [`crate::server::serve_one_connection`], which
//! wraps [`handshake::run`] in a [`tokio::time::timeout`]. The
//! timeout duration is operator-controlled via the
//! `nbd.handshake_timeout_seconds` config key.

pub mod handshake;
pub mod transmission;
pub mod wire;
