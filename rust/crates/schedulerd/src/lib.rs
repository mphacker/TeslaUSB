//! `schedulerd` — the lock-chime scheduler daemon's library core.
//!
//! This crate owns the scheduler's **state** (the chime library index, the
//! schedules, the groups, and the random-on-boot config) and the **decision**
//! of which chime should be active at any instant. It is the single writer of
//! that state, mirroring the project's ownership discipline (`gadgetd` owns the
//! USB write queue; `schedulerd` owns the schedule state). `webd` is a pure
//! proxy that forwards REST requests to this daemon.
//!
//! ## Layering
//!
//! * [`teslausb_core::chime`] — the pure, I/O-free rule engine + calendar math.
//! * [`model`] — the wire/persisted DTOs and the validation boundary that
//!   lowers untrusted input into the engine types.
//! * [`store`] — the versioned, atomically-persisted state and its CRUD +
//!   evaluation operations.
//!
//! The **live wiring** — a UDS control socket (mirroring `gadgetd`'s framing),
//! the per-minute tick that evaluates [`store::Store::evaluate`], and the
//! `gadgetd` activation enqueue that swaps `LockChime.wav` — is hardware/IPC
//! gated and is built in a later slice, exactly like `uploadd`/`retentiond`.
//! Everything in this crate is host-unit-tested.

pub mod model;
pub mod store;
