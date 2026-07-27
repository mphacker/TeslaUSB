//! The **`rclone`-backed transfer engine** — the chosen v1 upload backend, behind
//! a host-testable subprocess seam.
//!
//! [`crate::transfer`] leaves the backend a "choose at build" decision and keeps
//! the chunk-streaming [`crate::transfer::Uploader`] seam for an in-process Rust
//! uploader. For v1 the decision is made: **shell out to `rclone`** for provider
//! breadth and parity with the Python reference. `rclone` transfers a *whole
//! file* in one invocation (`rclone copyto`), self-enforces the `WiFi` TX cap with
//! `--bwlimit`, and computes the remote digest with `rclone hashsum` — so the
//! chunk-level [`crate::transfer::Uploader`] (per-offset `put_chunk` + in-process
//! [`crate::throttle::Pacer`]) is a poor fit. This module therefore implements the
//! *whole-item* contract directly: it is an [`crate::serve::UploadProcessor`] (the
//! same per-item contract [`crate::engine::UploadEngine`] satisfies), so the
//! [`crate::serve::Scheduler`] drives it unchanged.
//!
//! # The trait seam ([`CommandRunner`])
//!
//! Every `rclone` invocation goes through [`CommandRunner`], a tiny "run a
//! program, capture its output" trait. The live impl spawns the real `rclone`
//! binary (under `nice`/`ionice` in the gated wiring); tests inject a fake runner
//! that returns scripted output, so the whole flow — copy, hash, verify, commit,
//! retry, lease denial, throttle pause — is host-unit-tested with **no
//! subprocess and no network**.
//!
//! # Invariants upheld (identical to [`crate::engine`])
//!
//! - **Source only from the archive.** The source path is resolved through
//!   [`crate::source::ArchiveRoot::resolve`]; a rejected path fails the item
//!   (retryable) and `rclone` is never invoked — the live car LUN is unreachable.
//! - **Never delete.** There is no remove path.
//! - **Never exceed the cap.** `rclone` is invoked with `--bwlimit` seeded from
//!   the `wifid`-published `max_tx_bytes_per_s`; if the gate says pause, `rclone`
//!   is never spawned.
//! - **Never evict mid-read.** An upload lease is held across the whole
//!   invocation and released on every exit path.
//!
//! # Lease renewal limitation (flagged for the live lane)
//!
//! `rclone copyto` is a single blocking subprocess, so a multi-minute transfer
//! cannot renew its lease *mid-copy* from this single-threaded core. The engine
//! renews once between the copy and the (separate, also potentially slow) hashsum
//! invocation, which covers the common case; a transfer longer than the lease TTL
//! needs a background renewal thread (or `rclone --progress` parsing) in the live
//! wiring. This is a wiring concern, not a core-logic one, and is recorded as
//! such.

use crate::config::UploaddConfig;
use crate::engine::StepOutcome;
use crate::error::EngineError;
use crate::lease::{LeaseClient, LeaseGen, LeaseGrant, LeaseId, LeaseKind, RenewResult};
use crate::queue::{CommitEvidence, QueueItem, QueueStore, attempt_id_for};
use crate::serve::UploadProcessor;
use crate::source::{ArchiveItemId, ArchivePath, ArchiveRoot};
use crate::throttle::{ThrottleSource, UploadGate};
use crate::transfer::{Integrity, RemoteVerify, VerifyAlg, VerifySpec, verify_digest};

/// Captured result of one external command invocation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandOutput {
    /// Process exit code (`0` is success; any non-zero is a failure).
    pub status: i32,
    /// Captured standard output.
    pub stdout: String,
    /// Captured standard error.
    pub stderr: String,
}

/// The subprocess seam: run a program with arguments and capture its output. The
/// live impl spawns the real binary (with `nice`/`ionice` in the gated wiring);
/// tests inject a deterministic fake.
pub trait CommandRunner {
    /// Run `program` with `args`, blocking until it exits, and return the
    /// captured [`CommandOutput`].
    ///
    /// # Errors
    /// Returns a human-readable reason if the process could not be spawned or
    /// awaited (a spawn failure is distinct from a non-zero exit, which is a
    /// successful run that the caller inspects via [`CommandOutput::status`]).
    fn run(&self, program: &str, args: &[String]) -> Result<CommandOutput, String>;
}

/// Static `rclone` remote configuration: where the binary is, which configured
/// remote to target, and an optional explicit config file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RcloneRemote {
    /// Path to (or name of) the `rclone` binary, e.g. `/usr/bin/rclone`.
    pub binary: String,
    /// Configured remote name (the `name:` prefix in `rclone.conf`), e.g.
    /// `teslausb-cloud`.
    pub name: String,
    /// Optional explicit `rclone.conf` path (passed as `--config`). When `None`,
    /// `rclone` uses its default config location.
    pub config_path: Option<String>,
}

/// A lease currently held for one in-flight transfer.
struct HeldLease {
    lease_id: LeaseId,
    gen_token: LeaseGen,
}

/// Why an `rclone` transfer stopped before producing a verified digest.
enum RcloneStop {
    /// A recoverable failure (spawn error, non-zero exit, unparseable hash). The
    /// item is parked for retry; `rclone copyto` is itself overwriting, so a
    /// retry simply re-runs the whole copy.
    Recoverable(String),
    /// The lease was lost (a `Stale` renew) — stop and re-acquire later.
    LeaseLost(String),
    /// The remote digest did not match the expected hash (corrupt/partial). The
    /// whole file re-uploads on retry.
    Corrupt(String),
}

/// The `rclone`-backed, whole-file upload engine.
///
/// Fields are public so the engine is assembled with a struct literal (avoiding a
/// wide constructor); each is a borrowed seam resolved by the live binary or a
/// test mock.
pub struct RcloneUploadEngine<'a> {
    /// Tunable policy (lease TTL, retry cap, holder id).
    pub cfg: &'a UploaddConfig,
    /// The archive root every source read is confined under.
    pub archive_root: &'a ArchiveRoot,
    /// Static `rclone` remote configuration.
    pub remote: &'a RcloneRemote,
    /// The subprocess seam used to invoke `rclone`.
    pub runner: &'a dyn CommandRunner,
    /// Lease acquire/renew/release seam (`indexd`).
    pub lease: &'a dyn LeaseClient,
    /// Durable queue persistence seam (`indexd`).
    pub queue_store: &'a dyn QueueStore,
    /// Combined `wifid` + `retentiond` throttle source.
    pub throttle: &'a dyn ThrottleSource,
}

impl UploadProcessor for RcloneUploadEngine<'_> {
    fn process(&self, item: &mut QueueItem) -> Result<StepOutcome, EngineError> {
        RcloneUploadEngine::process(self, item)
    }
}

impl RcloneUploadEngine<'_> {
    /// Process a single item end-to-end via `rclone`: throttle-gate, resolve the
    /// archive path, acquire an upload lease, `rclone copyto` (paced by
    /// `--bwlimit`), verify the remote digest, commit evidence, and always
    /// release the lease.
    ///
    /// # Errors
    /// Returns an [`EngineError`] only on an infrastructure failure (a queue-store
    /// RPC error). Transfer / integrity / lease failures are
    /// reported as [`StepOutcome::Retry`] / [`StepOutcome::Exhausted`] /
    /// [`StepOutcome::SkippedLeaseDenied`], never as errors.
    pub fn process(&self, item: &mut QueueItem) -> Result<StepOutcome, EngineError> {
        let max_tx = match self.throttle.current().gate() {
            UploadGate::Pause { action, reason } => {
                return Ok(StepOutcome::Paused { reason, action });
            }
            UploadGate::Run {
                max_tx_bytes_per_s, ..
            } => max_tx_bytes_per_s,
        };

        // Resolve under the archive root *before* taking a lease: a rejected path
        // is a retryable item failure that must never reach `rclone`.
        let path = match self.archive_root.resolve(&item.source_rel) {
            Ok(path) => path,
            Err(err) => return self.fail(item, &format!("source path rejected: {err}"), false),
        };

        let held = match self.acquire(item.archive_item_id) {
            Ok(held) => held,
            Err(reason) => {
                return Ok(StepOutcome::SkippedLeaseDenied {
                    item: item.archive_item_id,
                    reason,
                });
            }
        };

        // Mark in-flight and persist before spawning so a crash leaves a
        // resumable row. Release the lease if that persist fails.
        item.begin();
        if let Err(err) = self.queue_store.persist(item) {
            let _ = self.lease.release(held.lease_id, held.gen_token);
            return Err(EngineError::Index(err));
        }

        let result = self.transfer_and_verify(&path, item, &held, max_tx);

        // Always release, on every path out (best effort).
        let _ = self.lease.release(held.lease_id, held.gen_token);

        match result {
            Ok((hash, hash_alg)) => self.finish_verified(item, hash, hash_alg),
            Err(RcloneStop::Corrupt(reason)) => self.fail(item, &reason, true),
            Err(RcloneStop::Recoverable(reason) | RcloneStop::LeaseLost(reason)) => {
                self.fail(item, &reason, false)
            }
        }
    }

    /// Acquire an upload lease, returning the held lease or a denial reason.
    fn acquire(&self, id: ArchiveItemId) -> Result<HeldLease, String> {
        match self.lease.acquire(
            id,
            LeaseKind::Upload,
            &self.cfg.holder_id,
            self.cfg.lease.ttl_ms,
        ) {
            LeaseGrant::Granted {
                lease_id,
                gen_token,
                expires_mono_ms: _,
            } => Ok(HeldLease {
                lease_id,
                gen_token,
            }),
            LeaseGrant::Denied { reason } => Err(reason),
        }
    }

    /// Run `rclone copyto`, renew the lease, fetch + verify the remote digest.
    fn transfer_and_verify(
        &self,
        path: &ArchivePath,
        item: &QueueItem,
        held: &HeldLease,
        max_tx: u64,
    ) -> Result<(String, String), RcloneStop> {
        self.run_copy(path, item, max_tx)?;
        // Renew once between the (potentially long) copy and the (also slow)
        // hashsum so a single TTL covers both halves. See the module-level note
        // on the mid-copy renewal limitation.
        self.renew(held)?;
        let remote_verify = self.remote_verify(item)?;
        match verify_digest(&item.verify, &remote_verify, item.total_bytes) {
            Integrity::Verified => match remote_verify {
                RemoteVerify::Native { alg, value } => Ok((value, alg.as_str().to_owned())),
                RemoteVerify::CopyIntegrity { .. } => Ok((String::new(), "none".to_owned())),
            },
            Integrity::Corrupt => Err(RcloneStop::Corrupt(
                "integrity check failed: remote verification did not match expected spec"
                    .to_owned(),
            )),
        }
    }

    /// `rclone [--config C] copyto <src> <remote:key> --bwlimit <max_tx>B
    /// --buffer-size 0 --retries 1`.
    fn run_copy(
        &self,
        path: &ArchivePath,
        item: &QueueItem,
        max_tx: u64,
    ) -> Result<(), RcloneStop> {
        let mut args = self.base_args();
        args.push("copyto".to_owned());
        args.push(path.as_str().to_owned());
        args.push(self.remote_dest(&item.key.remote_key));
        args.push("--bwlimit".to_owned());
        // The `B` suffix makes `rclone` read the limit as bytes/sec (a bare
        // number would be KiB/sec); this is the same cap `wifid` enforces in the
        // kernel, so the belt and braces agree.
        args.push(format!("{max_tx}B"));
        // Drop the read-ahead buffer: it saves ~6 MB RSS on the 415 MB target
        // (spike `vs-0b`) and buys nothing, since `--bwlimit` already paces the
        // transfer far below disk read speed.
        args.push("--buffer-size".to_owned());
        args.push("0".to_owned());
        // This queue owns retry, backoff and the indexd attempts ledger. Leaving
        // `rclone` to retry internally (default 3) would burn attempts invisibly
        // to that ledger and spend up to 3x the TX budget the cap protects.
        args.push("--retries".to_owned());
        args.push("1".to_owned());
        let out = self
            .runner
            .run(&self.remote.binary, &args)
            .map_err(RcloneStop::Recoverable)?;
        if out.status != 0 {
            return Err(RcloneStop::Recoverable(format!(
                "rclone copyto exited {}: {}",
                out.status,
                first_line(&out.stderr)
            )));
        }
        Ok(())
    }

    fn remote_verify(&self, item: &QueueItem) -> Result<RemoteVerify, RcloneStop> {
        match &item.verify {
            VerifySpec::Native { alg, .. } => {
                let value = self.remote_hashsum(item, *alg)?;
                Ok(RemoteVerify::Native { alg: *alg, value })
            }
            VerifySpec::CopyIntegrity => {
                let size_bytes = self.remote_size(item)?;
                Ok(RemoteVerify::CopyIntegrity { size_bytes })
            }
        }
    }

    /// `rclone [--config C] hashsum <alg> <remote:key>`, parsed to the first hash
    /// token.
    fn remote_hashsum(&self, item: &QueueItem, alg: VerifyAlg) -> Result<String, RcloneStop> {
        let mut args = self.base_args();
        args.push("hashsum".to_owned());
        args.push(alg.to_string());
        args.push(self.remote_dest(&item.key.remote_key));
        let out = self
            .runner
            .run(&self.remote.binary, &args)
            .map_err(RcloneStop::Recoverable)?;
        if out.status != 0 {
            return Err(RcloneStop::Recoverable(format!(
                "rclone hashsum exited {}: {}",
                out.status,
                first_line(&out.stderr)
            )));
        }
        parse_hashsum_value(&out.stdout).ok_or_else(|| {
            RcloneStop::Recoverable(format!(
                "could not parse rclone hashsum output: {:?}",
                first_line(&out.stdout)
            ))
        })
    }

    /// `rclone [--config C] size --json <remote:key>`, parsed to bytes.
    fn remote_size(&self, item: &QueueItem) -> Result<u64, RcloneStop> {
        let mut args = self.base_args();
        args.push("size".to_owned());
        args.push("--json".to_owned());
        args.push(self.remote_dest(&item.key.remote_key));
        let out = self
            .runner
            .run(&self.remote.binary, &args)
            .map_err(RcloneStop::Recoverable)?;
        if out.status != 0 {
            return Err(RcloneStop::Recoverable(format!(
                "rclone size exited {}: {}",
                out.status,
                first_line(&out.stderr)
            )));
        }
        parse_size_bytes(&out.stdout).ok_or_else(|| {
            RcloneStop::Recoverable(format!(
                "could not parse rclone size output: {:?}",
                first_line(&out.stdout)
            ))
        })
    }

    /// Renew the held lease for a further TTL; a `Stale` result loses the lease.
    fn renew(&self, held: &HeldLease) -> Result<(), RcloneStop> {
        match self
            .lease
            .renew(held.lease_id, held.gen_token, self.cfg.lease.ttl_ms)
        {
            RenewResult::Renewed { expires_mono_ms: _ } => Ok(()),
            RenewResult::Stale { reason } => Err(RcloneStop::LeaseLost(reason)),
        }
    }

    /// On a verified upload, durably commit backend evidence then complete.
    fn finish_verified(
        &self,
        item: &mut QueueItem,
        hash: String,
        hash_alg: String,
    ) -> Result<StepOutcome, EngineError> {
        let evidence = CommitEvidence {
            attempt_id: attempt_id_for(&item.key, item.attempts),
            hash,
            hash_alg,
            size: item.total_bytes,
            upload_set_id: None,
        };
        self.queue_store.commit(item, &evidence)?;
        item.complete();
        Ok(StepOutcome::Uploaded {
            item: item.archive_item_id,
            bytes: item.total_bytes,
        })
    }

    /// Apply a failure to the item, persist it, and report Retry vs Exhausted.
    fn fail(
        &self,
        item: &mut QueueItem,
        reason: &str,
        reset_offset: bool,
    ) -> Result<StepOutcome, EngineError> {
        item.fail(reason, reset_offset);
        self.queue_store.persist(item)?;
        if item.is_retryable(self.cfg.retry.max_attempts) {
            Ok(StepOutcome::Retry {
                item: item.archive_item_id,
                reason: reason.to_owned(),
            })
        } else {
            Ok(StepOutcome::Exhausted {
                item: item.archive_item_id,
                reason: reason.to_owned(),
            })
        }
    }

    /// The leading args common to every invocation (`--config` if configured).
    fn base_args(&self) -> Vec<String> {
        let mut args = Vec::new();
        if let Some(conf) = &self.remote.config_path {
            args.push("--config".to_owned());
            args.push(conf.clone());
        }
        args
    }

    /// The `remote:key` destination spec for `rclone`.
    fn remote_dest(&self, remote_key: &str) -> String {
        format!("{}:{remote_key}", self.remote.name)
    }
}

/// The first non-empty trimmed line of `s` (for compact error/diagnostic text).
fn first_line(s: &str) -> String {
    s.lines().next().unwrap_or("").trim().to_owned()
}

/// Parse the leading hashsum token from one `rclone hashsum` output line
/// (`"<hash>  <path>"`).
fn parse_hashsum_value(stdout: &str) -> Option<String> {
    let token = stdout.split_whitespace().next()?;
    if token.is_empty() || token.len() > 256 {
        return None;
    }
    Some(token.to_owned())
}

fn parse_size_bytes(stdout: &str) -> Option<u64> {
    let value: serde_json::Value = serde_json::from_str(stdout).ok()?;
    value.get("bytes")?.as_u64()
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::indexing_slicing
)]
mod tests {
    use std::cell::RefCell;

    use super::{CommandOutput, CommandRunner, RcloneRemote, RcloneUploadEngine};
    use crate::config::UploaddConfig;
    use crate::engine::StepOutcome;
    use crate::error::IndexError;
    use crate::lease::{
        LeaseClient, LeaseGen, LeaseGrant, LeaseId, LeaseKind, ReleaseResult, RenewResult,
    };
    use crate::priority::UploadCategory;
    use crate::queue::{CommitEvidence, QueueItem, QueueKey, QueueStore, UploadState, attempt_id_for};
    use crate::source::{ArchiveItemId, ArchiveRoot};
    use crate::throttle::{
        LinkMode, PauseAction, PauseReason, StoragePressure, ThrottleSnapshot, ThrottleSource,
        WifiThrottle,
    };
    use crate::transfer::{VerifyAlg, VerifySpec};

    fn expected_sha256() -> String {
        "07".repeat(32)
    }

    fn verify_sha256() -> VerifySpec {
        VerifySpec::Native {
            alg: VerifyAlg::Sha256,
            expected: expected_sha256(),
        }
    }

    /// Scripted runner: returns chosen outputs for `copyto` / `hashsum` / `size`,
    /// and records every invocation's args.
    struct FakeRunner {
        copyto: CommandOutput,
        hashsum: CommandOutput,
        size: CommandOutput,
        spawn_err: Option<String>,
        calls: RefCell<Vec<Vec<String>>>,
    }

    impl FakeRunner {
        fn ok(hash_value: &str, size_bytes: u64) -> Self {
            Self {
                copyto: CommandOutput {
                    status: 0,
                    stdout: String::new(),
                    stderr: String::new(),
                },
                hashsum: CommandOutput {
                    status: 0,
                    stdout: format!("{hash_value}  remote/clip.mp4\n"),
                    stderr: String::new(),
                },
                size: CommandOutput {
                    status: 0,
                    stdout: format!(r#"{{"count":1,"bytes":{size_bytes}}}"#),
                    stderr: String::new(),
                },
                spawn_err: None,
                calls: RefCell::new(Vec::new()),
            }
        }
    }

    impl CommandRunner for FakeRunner {
        fn run(&self, _program: &str, args: &[String]) -> Result<CommandOutput, String> {
            self.calls.borrow_mut().push(args.to_vec());
            if let Some(err) = &self.spawn_err {
                return Err(err.clone());
            }
            if args.iter().any(|a| a == "copyto") {
                Ok(self.copyto.clone())
            } else if args.iter().any(|a| a == "hashsum") {
                Ok(self.hashsum.clone())
            } else if args.iter().any(|a| a == "size") {
                Ok(self.size.clone())
            } else {
                Err(format!("unexpected rclone args: {args:?}"))
            }
        }
    }

    /// Configurable lease client: grant or deny, and renew or go stale.
    struct FakeLease {
        deny: Option<String>,
        renew_stale: bool,
        released: RefCell<u32>,
    }

    impl FakeLease {
        fn granting() -> Self {
            Self {
                deny: None,
                renew_stale: false,
                released: RefCell::new(0),
            }
        }
    }

    impl LeaseClient for FakeLease {
        fn acquire(
            &self,
            _item: ArchiveItemId,
            _kind: LeaseKind,
            _holder: &str,
            _ttl_ms: i64,
        ) -> LeaseGrant {
            match &self.deny {
                Some(reason) => LeaseGrant::Denied {
                    reason: reason.clone(),
                },
                None => LeaseGrant::Granted {
                    lease_id: LeaseId(1),
                    gen_token: LeaseGen(42),
                    expires_mono_ms: crate::time::MonoMs(60_000),
                },
            }
        }

        fn renew(&self, _lease_id: LeaseId, _gen_token: LeaseGen, _ttl_ms: i64) -> RenewResult {
            if self.renew_stale {
                RenewResult::Stale {
                    reason: "gen mismatch".to_owned(),
                }
            } else {
                RenewResult::Renewed {
                    expires_mono_ms: crate::time::MonoMs(120_000),
                }
            }
        }

        fn release(&self, _lease_id: LeaseId, _gen_token: LeaseGen) -> ReleaseResult {
            *self.released.borrow_mut() += 1;
            ReleaseResult::Released
        }
    }

    #[derive(Default)]
    struct FakeStore {
        persists: RefCell<u32>,
        commits: RefCell<Vec<CommitEvidence>>,
        op_order: RefCell<Vec<&'static str>>,
    }

    impl QueueStore for FakeStore {
        fn load(&self) -> Result<Vec<QueueItem>, IndexError> {
            Ok(Vec::new())
        }

        fn persist(&self, _item: &QueueItem) -> Result<(), IndexError> {
            *self.persists.borrow_mut() += 1;
            self.op_order.borrow_mut().push("persist");
            Ok(())
        }

        fn commit(&self, _item: &QueueItem, evidence: &CommitEvidence) -> Result<(), IndexError> {
            self.commits.borrow_mut().push(evidence.clone());
            self.op_order.borrow_mut().push("commit");
            Ok(())
        }
    }

    /// Throttle source fixed to a chosen snapshot.
    struct FakeThrottle {
        snap: ThrottleSnapshot,
    }

    impl FakeThrottle {
        fn running() -> Self {
            Self {
                snap: ThrottleSnapshot {
                    wifi: WifiThrottle {
                        seq: 1,
                        link_mode: LinkMode::Sta,
                        uploads_allowed: true,
                        max_tx_bytes_per_s: 1_048_576,
                        max_chunk_bytes: 256 * 1024,
                        action: PauseAction::Run,
                        reason: PauseReason::None,
                    },
                    storage: StoragePressure::open(),
                },
            }
        }

        fn paused() -> Self {
            Self {
                snap: ThrottleSnapshot {
                    wifi: WifiThrottle::closed(),
                    storage: StoragePressure::open(),
                },
            }
        }
    }

    impl ThrottleSource for FakeThrottle {
        fn current(&self) -> ThrottleSnapshot {
            self.snap
        }
    }

    fn item() -> QueueItem {
        QueueItem::new(
            QueueKey::new("dest-a", "remote/clip.mp4"),
            ArchiveItemId(7),
            "clip.mp4",
            "SentryClips/clip.mp4",
            UploadCategory::EventSentry,
            0,
            1_000,
            verify_sha256(),
        )
    }

    fn remote() -> RcloneRemote {
        RcloneRemote {
            binary: "/usr/bin/rclone".to_owned(),
            name: "teslausb-cloud".to_owned(),
            config_path: Some("/etc/teslausb/rclone.conf".to_owned()),
        }
    }

    #[test]
    fn verified_upload_commits_and_completes() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let runner = FakeRunner::ok(&expected_sha256(), 1_000);
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        let outcome = engine.process(&mut it).unwrap();
        assert_eq!(
            outcome,
            StepOutcome::Uploaded {
                item: ArchiveItemId(7),
                bytes: 1_000
            }
        );
        assert_eq!(it.state, UploadState::Done);
        assert_eq!(
            store.commits.borrow().as_slice(),
            &[CommitEvidence {
                attempt_id: attempt_id_for(&it.key, it.attempts),
                hash: expected_sha256(),
                hash_alg: VerifyAlg::Sha256.as_str().to_owned(),
                size: it.total_bytes,
                upload_set_id: None,
            }]
        );
        let ops = store.op_order.borrow();
        let commit_index = ops
            .iter()
            .position(|op| *op == "commit")
            .expect("commit op recorded");
        assert!(
            !ops[commit_index + 1..].iter().any(|op| *op == "persist"),
            "verified path does not persist after commit"
        );
        assert_eq!(*lease.released.borrow(), 1, "lease always released");
    }

    #[test]
    fn copyto_args_carry_bwlimit_config_and_dest() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let runner = FakeRunner::ok(&expected_sha256(), 1_000);
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        engine.process(&mut it).unwrap();
        let calls = runner.calls.borrow();
        let copy = calls
            .iter()
            .find(|a| a.contains(&"copyto".to_owned()))
            .unwrap();
        assert!(copy.contains(&"--config".to_owned()));
        assert!(copy.contains(&"/etc/teslausb/rclone.conf".to_owned()));
        assert!(copy.contains(&"--bwlimit".to_owned()));
        assert!(copy.contains(&"1048576B".to_owned()));
        assert!(copy.contains(&"teslausb-cloud:remote/clip.mp4".to_owned()));
        assert!(copy.contains(&"/mnt/archive/SentryClips/clip.mp4".to_owned()));
    }

    /// The value following `flag`, so a test asserts an actual flag/value pair
    /// rather than the mere presence of a bare token like `"1"` somewhere in the
    /// vector (which `contains` would satisfy for the wrong reason).
    fn flag_value<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
        let i = args.iter().position(|a| a == flag)?;
        args.get(i + 1).map(String::as_str)
    }

    /// `--buffer-size 0` and `--retries 1` are pinned on every copy.
    ///
    /// `--buffer-size 0`: measured on the target (hardware spike `vs-0b`) to cut
    /// peak RSS 63 MB -> 57 MB on a 415 MB device. The read-ahead buffer buys
    /// nothing here because `--bwlimit` already paces the transfer well below
    /// disk read speed.
    ///
    /// `--retries 1` is a *correctness* requirement, not a tuning knob: this
    /// queue owns retry, backoff and the indexd attempts ledger. Letting
    /// `rclone` retry internally (its default is 3) would burn attempts
    /// invisibly to that ledger and spend up to 3x the TX budget the `WiFi` cap
    /// exists to protect.
    #[test]
    fn copyto_pins_buffer_size_and_retries() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let runner = FakeRunner::ok(&expected_sha256(), 1_000);
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        engine.process(&mut it).unwrap();
        let calls = runner.calls.borrow();
        let copy = calls
            .iter()
            .find(|a| a.contains(&"copyto".to_owned()))
            .unwrap();
        assert_eq!(flag_value(copy, "--buffer-size"), Some("0"));
        assert_eq!(flag_value(copy, "--retries"), Some("1"));
    }

    #[test]
    fn lease_denied_skips_without_invoking_rclone() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let runner = FakeRunner::ok(&expected_sha256(), 1_000);
        let lease = FakeLease {
            deny: Some("delete claimed".to_owned()),
            renew_stale: false,
            released: RefCell::new(0),
        };
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        let outcome = engine.process(&mut it).unwrap();
        assert!(matches!(outcome, StepOutcome::SkippedLeaseDenied { .. }));
        assert!(runner.calls.borrow().is_empty(), "rclone not invoked");
        assert_eq!(it.state, UploadState::Queued, "state untouched");
    }

    #[test]
    fn throttle_pause_skips_without_lease_or_rclone() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let runner = FakeRunner::ok(&expected_sha256(), 1_000);
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::paused();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        let outcome = engine.process(&mut it).unwrap();
        assert!(matches!(outcome, StepOutcome::Paused { .. }));
        assert!(runner.calls.borrow().is_empty());
        assert_eq!(*lease.released.borrow(), 0, "no lease taken");
    }

    #[test]
    fn copyto_failure_is_retry() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let mut runner = FakeRunner::ok(&expected_sha256(), 1_000);
        runner.copyto = CommandOutput {
            status: 1,
            stdout: String::new(),
            stderr: "Failed to copy: connection reset\n".to_owned(),
        };
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        match engine.process(&mut it).unwrap() {
            StepOutcome::Retry { reason, .. } => assert!(reason.contains("connection reset")),
            other => panic!("expected retry, got {other:?}"),
        }
        assert_eq!(it.state, UploadState::Failed);
        assert_eq!(it.attempts, 1);
        assert!(store.commits.borrow().is_empty(), "never committed");
        assert_eq!(*lease.released.borrow(), 1, "lease released on failure");
    }

    #[test]
    fn integrity_mismatch_is_retry() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        // Hash of all-zeroes does not match the expected 0x07 digest.
        let runner = FakeRunner::ok(&"00".repeat(32), 1_000);
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        match engine.process(&mut it).unwrap() {
            StepOutcome::Retry { reason, .. } => assert!(reason.contains("integrity")),
            other => panic!("expected retry, got {other:?}"),
        }
        assert!(store.commits.borrow().is_empty(), "corrupt upload not committed");
    }

    #[test]
    fn lease_lost_midway_is_retry() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let runner = FakeRunner::ok(&expected_sha256(), 1_000);
        let lease = FakeLease {
            deny: None,
            renew_stale: true,
            released: RefCell::new(0),
        };
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        match engine.process(&mut it).unwrap() {
            StepOutcome::Retry { reason, .. } => assert!(reason.contains("gen mismatch")),
            other => panic!("expected retry from lost lease, got {other:?}"),
        }
        // hashsum must not have run after the stale renew.
        assert!(
            !runner
                .calls
                .borrow()
                .iter()
                .any(|a| a.contains(&"hashsum".to_owned())),
            "hashsum should not run after lease loss"
        );
    }

    #[test]
    fn source_outside_archive_root_is_retry_without_lease() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let runner = FakeRunner::ok(&expected_sha256(), 1_000);
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = QueueItem::new(
            QueueKey::new("dest-a", "remote/x.mp4"),
            ArchiveItemId(9),
            "x.mp4",
            "../../mnt/cam/live/recent.mp4",
            UploadCategory::Bulk,
            0,
            10,
            verify_sha256(),
        );
        assert!(matches!(
            engine.process(&mut it).unwrap(),
            StepOutcome::Retry { .. }
        ));
        assert!(runner.calls.borrow().is_empty(), "rclone never invoked");
        assert_eq!(*lease.released.borrow(), 0, "no lease taken for bad path");
    }

    #[test]
    fn exhausted_after_max_attempts() {
        let mut cfg = UploaddConfig::default();
        cfg.retry.max_attempts = 1;
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let mut runner = FakeRunner::ok(&expected_sha256(), 1_000);
        runner.copyto = CommandOutput {
            status: 1,
            stdout: String::new(),
            stderr: "boom\n".to_owned(),
        };
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        assert!(matches!(
            engine.process(&mut it).unwrap(),
            StepOutcome::Exhausted { .. }
        ));
    }

    #[test]
    fn unparseable_hashsum_is_retry() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let mut runner = FakeRunner::ok(&expected_sha256(), 1_000);
        runner.hashsum = CommandOutput {
            status: 0,
            stdout: "\n".to_owned(),
            stderr: String::new(),
        };
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };
        let mut it = item();
        match engine.process(&mut it).unwrap() {
            StepOutcome::Retry { reason, .. } => assert!(reason.contains("hashsum")),
            other => panic!("expected retry, got {other:?}"),
        }
        assert!(store.commits.borrow().is_empty(), "missing hash does not commit");
    }

    #[test]
    fn parse_hashsum_value_round_trips() {
        let line = format!("{}  some/remote/path.mp4", "ab".repeat(32));
        assert_eq!(super::parse_hashsum_value(&line), Some("ab".repeat(32)));
        assert_eq!(super::parse_hashsum_value(""), None);
    }

    #[test]
    fn copy_integrity_commits_with_none_hash_evidence() {
        let cfg = UploaddConfig::default();
        let root = ArchiveRoot::new("/mnt/archive");
        let remote = remote();
        let runner = FakeRunner::ok("ignored", 1_000);
        let lease = FakeLease::granting();
        let store = FakeStore::default();
        let throttle = FakeThrottle::running();
        let engine = RcloneUploadEngine {
            cfg: &cfg,
            archive_root: &root,
            remote: &remote,
            runner: &runner,
            lease: &lease,
            queue_store: &store,
            throttle: &throttle,
        };

        let mut it = item();
        it.verify = VerifySpec::CopyIntegrity;
        let outcome = engine.process(&mut it).unwrap();
        assert!(matches!(outcome, StepOutcome::Uploaded { .. }));
        let calls = runner.calls.borrow();
        assert!(calls.iter().any(|args| args.contains(&"size".to_owned())));
        assert!(!calls.iter().any(|args| args.contains(&"hashsum".to_owned())));
        assert_eq!(
            store.commits.borrow().as_slice(),
            &[CommitEvidence {
                attempt_id: attempt_id_for(&it.key, it.attempts),
                hash: String::new(),
                hash_alg: "none".to_owned(),
                size: it.total_bytes,
                upload_set_id: None,
            }]
        );
        assert_eq!(
            store.commits.borrow()[0].hash,
            "",
            "hash must be empty when hash_alg is none"
        );
    }
}
