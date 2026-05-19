//! `teslafat` — userspace FAT/`exFAT` synthesizer + NBD server.
//!
//! Speaks the NBD newstyle protocol to a kernel `nbd-client`
//! which in turn backs the `g_mass_storage` USB gadget exposed
//! to the vehicle. The synthesized FAT/`exFAT` view is computed
//! from a real Linux directory tree (`backing_root`) at request
//! time; writes from the vehicle decode back into native file
//! operations.
//!
//! ## Phase 0.2 state
//!
//! Placeholder entry point — exits successfully so the empty
//! workspace builds and `cargo run -p teslafat` is a no-op. The
//! real bootstrap (clap CLI, TOML config loader, tracing init,
//! NBD listen socket, IPC control socket, signal handling) lands
//! in Phase 1.1 per `docs/00-PLAN.md`. Phase 1.3 ports
//! `teslafat/src/nbd/handshake.rs` (currently a design draft at
//! the repo root) into `src/nbd/handshake.rs` here.

fn main() {
    // Phase 0.2 placeholder. Replaced wholesale by Phase 1.1.
}
