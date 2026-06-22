//! `retentiond` — archiving, retention policy, and the SD-card space governor.
//!
//! This crate is the **host-testable core** of the retention daemon. It owns
//! three safety-critical jobs from [`docs/specs/retentiond.md`] and
//! [`docs/specs/storage.md`]:
//!
//! 1. **Per-folder archiving** — copy the car's `TeslaCam` footage off the
//!    car-visible LUN into the Pi-side ext4 archive, with each `TeslaCam` folder
//!    obeying its **own** policy (`SavedClips`, `SentryClips`, `RecentClips`,
//!    `TeslaTrackMode` are deliberately different — conflating them is the main
//!    correctness bug this exists to prevent).
//! 2. **The space governor** — a continuous watchdog that keeps the SD card from
//!    filling, evicting the **least-valuable safe** Pi-side item first while the
//!    OS / `gadgetd` / `SQLite` reserve stays **sacrosanct**, and **failing closed**
//!    (never deleting undurable user footage) under exhaustion.
//! 3. **Single-deleter, crash-safe deletion** — `retentiond` is the *sole*
//!    deleter of Pi-side archive files, honoring playback/upload leases and a
//!    rename-then-unlink protocol that is idempotent across power loss.
//!
//! # Architecture (mirrors `gadgetd`)
//!
//! Every decision — folder policy, manifest verification, the rotation estimate,
//! value scoring, the tier state machine, lease honoring, and the crash-safe
//! delete protocol — is **pure** and unit-tested over synthetic timelines. All
//! side effects go through traits (the I/O *seams*): [`io::Clock`],
//! [`io::ArchiveStore`], [`io::ArchiveDeleteOps`], [`io::Statfs`],
//! [`io::CarDeleteHandoff`], and [`io::IndexClient`]. The real Linux syscalls
//! (`statfs`, `fsync`, `rename`, recursive unlink) and the `gadgetd`/`indexd` IPC
//! clients live in the binary behind `#[cfg(unix)]`, so the policy core builds
//! and tests on any host.
//!
//! # Calibration gate (Task 2.7 / `storage.md` §7)
//!
//! No governor default (tier threshold, cadence, eviction weight) is shipped as
//! fact. They are explicit [`config`] values with provisional placeholders marked
//! `CALIBRATION-GATED`; the **logic is correct independent of the numbers**, and
//! calibration only sets the values.

pub mod archive;
pub mod archive_driver;
pub mod candidates;
pub mod config;
pub mod delete;
pub mod durability;
pub mod folder;
pub mod governor;
pub mod io;
pub mod lease;
pub mod manifest;
pub mod probe;
pub mod read_client;
pub mod recent;
pub mod register_client;
pub mod serve;
pub mod status;
pub mod time;
pub mod value;
