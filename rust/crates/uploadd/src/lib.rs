//! `uploadd` — the durable, resumable, throttled cloud-upload daemon
//! ([`docs/specs/uploadd.md`], Task 6.3).
//!
//! This crate is the **host-testable core** of the upload service. Everything
//! that decides *what* to upload, *in what order*, *how to stay under the `WiFi`
//! TX cap*, *how to resume after a crash without duplicating work*, and *how to
//! hold an upload lease so the space governor cannot evict a file mid-transfer*
//! is **pure** and unit-tested behind traits. The real side effects — the
//! transfer backend (rclone or a small Rust uploader — an open "choose at build"
//! decision, see [`transfer`]), the `indexd` lease/queue/durability RPC client,
//! the `wifid` throttle subscription, and the archive filesystem reads — live in
//! the gated binary, so the policy core builds and tests on any host.
//!
//! # Architecture (mirrors `gadgetd` / `retentiond`)
//!
//! The orchestration in [`engine`] is generic over I/O **seams**, each a trait:
//! [`source::ArchiveSource`] (archive reads), [`transfer::Uploader`] (the
//! transfer backend), [`lease::LeaseClient`] (acquire/renew/release via
//! `indexd`), [`queue::QueueStore`] (durable queue persistence via `indexd`,
//! the sole `SQLite` writer), [`durability::DurabilityClient`] (the
//! `UPLOADED_VERIFIED` flag via `indexd`), [`throttle::ThrottleSource`] (the
//! `wifid` cap + storage backpressure), and [`time::Clock`] / [`time::Waiter`]
//! (boot-scoped monotonic time + self-pacing). Tests drive deterministic mocks
//! for all of them.
//!
//! # Hard invariants this crate upholds ([`docs/specs/uploadd.md`] §3, §6)
//!
//! - **Source only from the archive.** A read is only ever issued through an
//!   [`source::ArchivePath`], which can only be built under the configured
//!   archive root — the live car LUN is **unreachable by construction**.
//! - **Never deletes Pi-side files.** There is *no* delete seam anywhere in this
//!   crate; deletion is `retentiond`'s sole responsibility
//!   ([`single-writer-lease.md`] §4). `uploadd` only flags durability.
//! - **Never exceeds the `WiFi` TX cap.** Transfers are paced by a token-bucket
//!   [`throttle::Pacer`] seeded from the `wifid`-published cap (the "belt"; the
//!   kernel `tc` cap is the "braces"), and stop entirely when uploads are not
//!   allowed (AP mode / chip recovery / link down).
//! - **Never reboots/restarts anything.** Failures are retried in-queue.
//!
//! # Calibration / convergence gates
//!
//! - The TX cap and chunk ceiling are **`// CALIBRATION-GATED (Task 2.6)`** —
//!   carried as [`config`] placeholders, never hardcoded real numbers
//!   ([`wifi-upload-throttle.md`] §3).
//! - The lease, time, and throttle types here **mirror** the shapes built in the
//!   parallel `retentiond` and `wifid` lanes so they converge cleanly onto the
//!   shared `teslausb-core::contracts` home later; divergences are flagged in the
//!   relevant module docs.
//!
//! [`docs/specs/uploadd.md`]: ../../../docs/specs/uploadd.md
//! [`single-writer-lease.md`]: ../../../docs/specs/contracts/single-writer-lease.md
//! [`wifi-upload-throttle.md`]: ../../../docs/specs/contracts/wifi-upload-throttle.md

pub mod config;
pub mod durability;
pub mod engine;
pub mod error;
pub mod lease;
pub mod priority;
pub mod queue;
pub mod source;
pub mod status;
pub mod throttle;
pub mod time;
pub mod transfer;
