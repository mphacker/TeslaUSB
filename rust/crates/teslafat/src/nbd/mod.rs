//! NBD (Network Block Device) protocol bits.
//!
//! Phase 1.3 lands the newstyle handshake in [`handshake`]. The
//! per-connection transmission loop (`READ` / `WRITE` / `FLUSH` /
//! `TRIM` dispatch over a `BlockBackend`) is Phase 1.5.
//!
//! # Phase 1.6 follow-up
//!
//! TODO(phase-1.6): wrap the [`handshake::run`] call in a
//! `tokio::time::timeout` (30 s is a reasonable starting point) so
//! a misbehaving client that opens a connection and then sends no
//! bytes cannot tie up a slot on the single-threaded runtime. The
//! threat model in Phase 1.3 is low (Unix socket, kernel-only
//! peer) so this is defence-in-depth, not a regression.
//!
//! # Phase 1.4 follow-up
//!
//! TODO(phase-1.4): once the `BlockBackend` trait lands in
//! `teslausb-core::backend`, this module doc should re-introduce
//! the intra-doc link to that trait in place of the inline
//! backticks above. Stripped for inc-1.3 because the broken-link
//! rustdoc lint is deny under workspace `-D warnings`.

pub mod handshake;
