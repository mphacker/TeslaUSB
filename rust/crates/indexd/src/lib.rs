//! `indexd` — R2 trips/events/clips derivation into `SQLite`
//! (`docs/specs/indexd.md`, contract D1
//! `docs/specs/contracts/indexd-schema.md`).
//!
//! Consumes the raw facts produced by [`scannerd`] (stable clips + SEI
//! sample streams) and turns them into the **derived domain model** the
//! web app reads — trips by day, event bubbles (speed / accel / braking
//! / sharp-turn / autopilot / sentry), per-event front-camera frame
//! mapping, and clip metadata — persisted in **`SQLite` (WAL)** on the
//! **Pi-side ext4 data filesystem**, never inside the car's `disk.img`
//! LUN or on the Tesla volume.
//!
//! ## Layering
//!
//! * [`geo`] — pure geometry: haversine + Ramer–Douglas–Peucker polyline
//!   simplification. Ported from v1's `mapping_trip_derivation.py`.
//! * [`derive`] — pure trip + event derivation over sampled waypoints.
//!   Thresholds ported verbatim from the v1 production materializer
//!   (`teslausb-worker`, ADR-0019) and the Python references.
//! * [`db`] — the `SQLite` store: schema, forward-only migrations, and the
//!   indexd-mediated mutation entry points (ingest, leases, delete-state,
//!   durability, WAL checkpoint). `indexd` is the **sole DB writer**.
//!
//! The derivation logic is pure and host-testable; SEI/MP4 byte parsing
//! is reused from `scannerd` + `teslausb-core` (never re-implemented
//! here), and the `SQLite` syscalls live behind the [`db`] store.

pub mod apply;
pub mod db;
pub mod derive;
pub mod geo;
pub mod model;
pub mod scan;
