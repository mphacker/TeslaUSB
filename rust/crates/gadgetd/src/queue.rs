//! Durable, coalescing mutation queue for the MEDIA (p2) write path.
//!
//! Every change to a car-facing partition costs a *handoff* (eject the LUN from
//! the host, loop-mount the image RW, apply, re-present). Doing that
//! synchronously and refusing whenever the host is connected (the old
//! `request_mutation` path) means uploads hard-fail in normal use: the car — or,
//! on the bench, the test PC — is almost always enumerated.
//!
//! This module makes writes *frictionless*: a validated mutation is accepted
//! immediately, persisted durably, and applied automatically at the next safe
//! window. It owns only the **pure** queue/coalescing/persistence logic; the
//! drain worker, idle-window detection and the actual handoff live in
//! [`crate::ipc`] / [`crate::handoff`] so this stays unit-testable on any host.
//!
//! # Why the queue lives in `gadgetd`
//! `gadgetd` is the single writer to the image and already owns the handoff lock
//! and serialization. Per the project's Hybrid-B rule, the daemon that owns a
//! resource owns its persistence; "pending writes to the image" is `gadgetd`'s
//! resource. `webd` stays a forwarder: it stages the upload to a persistent
//! root-owned dir, calls `enqueue_mutation`, and reports job state.
//!
//! # Coalescing (desired-state, not imperative)
//! Queued mutations are reconciled to a *desired end state* before a handoff, so
//! many pending changes collapse into ONE eject:
//! * per `(partition, path)` the highest-`seq` op wins;
//! * a path whose latest op is a delete joins one batched `DeletePaths`;
//! * a path whose latest op is an install keeps that `InstallFile`;
//! * every superseded earlier entry is marked [`MutationState::Coalesced`].
//!
//! This makes the active-chime swap naturally last-writer-wins, and a
//! delete-after-install (or install-after-delete) of the same path resolve to
//! the single latest intent.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::handoff::Mutation;
#[cfg(test)]
use crate::handoff::MAX_DELETE_PATHS;

/// Hard cap on live (non-terminal) queue entries. An `enqueue` past this is
/// refused with a real error so a runaway producer cannot fill the data fs with
/// staged blobs. A human managing six media categories never approaches this.
pub(crate) const MAX_QUEUE_ENTRIES: usize = 256;

/// Lifecycle of a single queued mutation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum MutationState {
    /// Accepted and durable; awaiting a safe apply window.
    Queued,
    /// Selected into the in-flight batch; the handoff is running.
    Applying,
    /// Successfully applied to the image (terminal).
    Applied,
    /// Superseded by a later mutation to the same path (terminal, never applied).
    Coalesced,
    /// Permanently rejected during apply (terminal). Transient busy never lands
    /// here — it stays `Queued`.
    FailedFatal,
}

impl MutationState {
    /// Terminal states are eligible for pruning and never re-applied.
    pub(crate) fn is_terminal(self) -> bool {
        matches!(self, Self::Applied | Self::Coalesced | Self::FailedFatal)
    }
}

/// One durable queue entry.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct QueuedMutation {
    /// Monotonic ordering key (assigned at enqueue; survives restarts).
    pub seq: u64,
    /// Stable public id (`m-<seq>`) echoed to `webd`/SPA as the job id.
    pub id: String,
    /// Target partition wire index (1 = `TeslaCam`, 2 = media).
    pub partition: u8,
    /// The validated desired-state mutation.
    pub mutation: Mutation,
    /// Absolute path of the persisted staged blob backing an `InstallFile`
    /// (root-owned, unlinked only after the entry reaches a terminal state).
    pub blob_path: Option<String>,
    /// Caller-supplied dedupe key; a repeat enqueue with the same live key is a
    /// no-op that returns the existing entry.
    pub idempotency_key: Option<String>,
    /// Wall-clock enqueue time (ms since epoch) for ordering/observability.
    pub enqueued_at_ms: u64,
    /// Current lifecycle state.
    pub state: MutationState,
}

/// The reconciled work for one partition's next handoff.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub(crate) struct BatchPlan {
    /// Mutations to apply in ONE handoff, delete(s) batched first.
    pub applies: Vec<Mutation>,
    /// `seq`s moving to `Applying` (the entries backing `applies`).
    pub apply_seqs: Vec<u64>,
    /// For each entry in `applies`, the queue `seq`s that contributed to it.
    /// `apply_seq_groups[i]` are the seqs backing `applies[i]`.
    /// Each winning delete `seq` appears in exactly one group.
    pub apply_seq_groups: Vec<Vec<u64>>,
    /// `seq`s superseded by a later same-path entry → `Coalesced`.
    pub coalesced_seqs: Vec<u64>,
}

impl BatchPlan {
    /// True when there is nothing to apply or coalesce.
    pub(crate) fn is_empty(&self) -> bool {
        self.applies.is_empty() && self.coalesced_seqs.is_empty()
    }
}

/// The durable mutation queue (in-memory model + JSON-journal persistence).
#[derive(Debug, Default, Serialize, Deserialize)]
pub(crate) struct MutationQueue {
    next_seq: u64,
    entries: Vec<QueuedMutation>,
}

/// The per-path effect a single entry contributes (for coalescing).
#[derive(Clone, Copy, PartialEq, Eq)]
enum Effect {
    Install,
    Delete,
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}

impl MutationQueue {
    /// Live (non-terminal) entry count.
    pub(crate) fn live_len(&self) -> usize {
        self.entries
            .iter()
            .filter(|e| !e.state.is_terminal())
            .count()
    }

    /// All entries (any state) — for status reporting.
    pub(crate) fn entries(&self) -> &[QueuedMutation] {
        &self.entries
    }

    /// Look up an entry by its public id.
    pub(crate) fn find(&self, id: &str) -> Option<&QueuedMutation> {
        self.entries.iter().find(|e| e.id == id)
    }

    /// Accept a validated mutation. Returns the (possibly pre-existing, via
    /// idempotency key) entry's id.
    ///
    /// # Errors
    /// Refuses with a reason when the live queue is at [`MAX_QUEUE_ENTRIES`].
    pub(crate) fn enqueue(
        &mut self,
        partition: u8,
        mutation: Mutation,
        blob_path: Option<String>,
        idempotency_key: Option<String>,
    ) -> Result<String, String> {
        if let Some(key) = idempotency_key.as_deref() {
            if let Some(existing) = self
                .entries
                .iter()
                .find(|e| !e.state.is_terminal() && e.idempotency_key.as_deref() == Some(key))
            {
                return Ok(existing.id.clone());
            }
        }
        if self.live_len() >= MAX_QUEUE_ENTRIES {
            return Err(format!(
                "queue full ({MAX_QUEUE_ENTRIES} pending changes); retry once some apply"
            ));
        }
        self.next_seq += 1;
        let seq = self.next_seq;
        let id = format!("m-{seq}");
        self.entries.push(QueuedMutation {
            seq,
            id: id.clone(),
            partition,
            mutation,
            blob_path,
            idempotency_key,
            enqueued_at_ms: now_ms(),
            state: MutationState::Queued,
        });
        Ok(id)
    }

    /// The partitions that currently have `Queued` work, lowest wire index first.
    pub(crate) fn pending_partitions(&self) -> Vec<u8> {
        let mut parts: Vec<u8> = self
            .entries
            .iter()
            .filter(|e| e.state == MutationState::Queued)
            .map(|e| e.partition)
            .collect();
        parts.sort_unstable();
        parts.dedup();
        parts
    }

    /// Reconcile all `Queued` entries for `partition` into a single-handoff plan.
    ///
    /// Deletes are emitted as one-or-more batched [`Mutation::DeletePaths`] (each
    /// capped by the mutation's own bound, with whole per-seq winning groups kept
    /// intact); installs keep their
    /// own entry so each carries its staged blob. Superseded earlier entries are
    /// reported for a `Coalesced` transition.
    pub(crate) fn plan_batch(&self, partition: u8) -> BatchPlan {
        // Highest-seq effect per path wins; remember which entry produced it.
        let mut latest: BTreeMap<String, (u64, Effect)> = BTreeMap::new();
        let mut queued: Vec<&QueuedMutation> = self
            .entries
            .iter()
            .filter(|e| e.state == MutationState::Queued && e.partition == partition)
            .collect();
        queued.sort_by_key(|e| e.seq);

        for entry in &queued {
            for (path, effect) in entry_effects(&entry.mutation) {
                latest
                    .entry(path)
                    .and_modify(|cur| {
                        if entry.seq >= cur.0 {
                            *cur = (entry.seq, effect);
                        }
                    })
                    .or_insert((entry.seq, effect));
            }
        }

        // An entry is fully "winning" iff every path it touches still names this
        // entry as the latest. Otherwise it is superseded (coalesced).
        let mut apply_seqs = Vec::new();
        let mut coalesced_seqs = Vec::new();
        let mut seq_paths: BTreeMap<u64, Vec<String>> = BTreeMap::new();
        let mut installs: Vec<(Mutation, u64)> = Vec::new();
        // Directory prunes have no path effect (so the `wins_any` test below
        // would wrongly coalesce them). They are always applied; dedup by path,
        // preserving first-seen order.
        let mut remove_dirs: Vec<String> = Vec::new();
        let mut remove_dir_seen: BTreeSet<String> = BTreeSet::new();
        let mut remove_dir_groups: BTreeMap<String, BTreeSet<u64>> = BTreeMap::new();

        for entry in &queued {
            if let Mutation::RemoveEmptyDir { rel_path } = &entry.mutation {
                apply_seqs.push(entry.seq);
                remove_dir_groups
                    .entry(rel_path.clone())
                    .or_default()
                    .insert(entry.seq);
                if remove_dir_seen.insert(rel_path.clone()) {
                    remove_dirs.push(rel_path.clone());
                }
                continue;
            }
            let mut wins_any = false;
            for (path, _effect) in entry_effects(&entry.mutation) {
                if latest.get(&path).is_some_and(|(seq, _)| *seq == entry.seq) {
                    wins_any = true;
                }
            }
            if wins_any {
                apply_seqs.push(entry.seq);
            } else {
                coalesced_seqs.push(entry.seq);
            }
        }

        // Build the minimal apply set from the winning per-path effects.
        for (path, (seq, effect)) in &latest {
            match effect {
                Effect::Delete => {
                    seq_paths.entry(*seq).or_default().push(path.clone());
                }
                Effect::Install => {
                    if let Some(src) = winning_install_source(&queued, path) {
                        installs.push((
                            Mutation::InstallFile {
                                rel_path: path.clone(),
                                source_path: src,
                            },
                            *seq,
                        ));
                    }
                }
            }
        }

        let mut applies = Vec::new();
        let mut apply_seq_groups = Vec::new();
        for (seq, mut rel_paths) in seq_paths {
            rel_paths.sort();
            applies.push(Mutation::DeletePaths { rel_paths });
            apply_seq_groups.push(vec![seq]);
        }
        // Directory prunes run AFTER the file deletes (each apply is its own
        // handoff/eject, applied in order) so the folder's files are already gone
        // when the empty-only `remove_dir` runs, and BEFORE installs.
        for rel_path in remove_dirs {
            let group = remove_dir_groups
                .get(&rel_path)
                .map(|seqs| seqs.iter().copied().collect())
                .unwrap_or_default();
            applies.push(Mutation::RemoveEmptyDir { rel_path });
            apply_seq_groups.push(group);
        }
        for (mutation, seq) in installs {
            applies.push(mutation);
            apply_seq_groups.push(vec![seq]);
        }

        debug_assert_group_coverage(&apply_seq_groups, &apply_seqs);

        BatchPlan {
            applies,
            apply_seqs,
            apply_seq_groups,
            coalesced_seqs,
        }
    }

    /// Move the given `seq`s to `state` (used by the drain worker as a batch
    /// transitions queued → applying → applied/failed, and to record coalesced).
    pub(crate) fn set_state(&mut self, seqs: &[u64], state: MutationState) {
        for entry in &mut self.entries {
            if seqs.contains(&entry.seq) {
                entry.state = state;
            }
        }
    }

    /// Blob paths whose entries just reached a terminal state and can be
    /// unlinked. Call after `set_state(.., Applied|Coalesced|FailedFatal)`.
    pub(crate) fn reclaimable_blobs(&self, seqs: &[u64]) -> Vec<String> {
        self.entries
            .iter()
            .filter(|e| seqs.contains(&e.seq) && e.state.is_terminal())
            .filter_map(|e| e.blob_path.clone())
            .collect()
    }

    /// Drop terminal entries from the in-memory journal (after their blobs are
    /// reclaimed) so it does not grow unbounded.
    pub(crate) fn prune_terminal(&mut self) {
        self.entries.retain(|e| !e.state.is_terminal());
    }

    /// Crash-recovery: flip any entry left `Applying` by an interrupted handoff
    /// back to `Queued` so the drain worker retries it. Safe because the handoff
    /// is idempotent at the desired-state level (crash-recovery re-presents the
    /// LUN, and re-applying an already-applied install/delete is a no-op-or-same
    /// outcome). Returns how many entries were requeued. Call once on startup,
    /// right after [`load`].
    pub(crate) fn requeue_inflight(&mut self) -> usize {
        let mut requeued = 0;
        for entry in &mut self.entries {
            if entry.state == MutationState::Applying {
                entry.state = MutationState::Queued;
                requeued += 1;
            }
        }
        requeued
    }

    /// Load a queue from its JSON journal, or an empty queue if absent/corrupt.
    /// A corrupt journal is logged and treated as empty rather than crashing the
    /// daemon (the staged blobs may still exist but are reclaimed lazily).
    pub(crate) fn load(path: &Path) -> Self {
        match std::fs::read(path) {
            Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_else(|e| {
                eprintln!(
                    "gadgetd queue: journal {} unreadable ({e}); starting empty",
                    path.display()
                );
                Self::default()
            }),
            Err(ref e) if e.kind() == io::ErrorKind::NotFound => Self::default(),
            Err(e) => {
                eprintln!(
                    "gadgetd queue: journal {} read failed ({e}); starting empty",
                    path.display()
                );
                Self::default()
            }
        }
    }

    /// Persist the queue atomically: write a sibling temp file, fsync it, rename
    /// over the journal, then fsync the directory so the rename is durable.
    ///
    /// # Errors
    /// Propagates the first I/O error; the caller keeps the in-memory state.
    pub(crate) fn persist(&self, path: &Path) -> io::Result<()> {
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        std::fs::create_dir_all(parent)?;
        let tmp = with_tmp_suffix(path);
        let bytes = serde_json::to_vec_pretty(self).map_err(io::Error::other)?;
        {
            let mut f = std::fs::File::create(&tmp)?;
            f.write_all(&bytes)?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, path)?;
        if let Ok(dir) = std::fs::File::open(parent) {
            let _ = dir.sync_all();
        }
        Ok(())
    }
}

fn debug_assert_group_coverage(groups: &[Vec<u64>], apply_seqs: &[u64]) {
    let grouped: BTreeSet<u64> = groups.iter().flatten().copied().collect();
    let apply_set: BTreeSet<u64> = apply_seqs.iter().copied().collect();
    debug_assert_eq!(grouped, apply_set);
}

/// The per-path effects a mutation contributes (path → install/delete).
fn entry_effects(mutation: &Mutation) -> Vec<(String, Effect)> {
    match mutation {
        Mutation::InstallFile { rel_path, .. } => vec![(rel_path.clone(), Effect::Install)],
        Mutation::DeletePath { rel_path } => vec![(rel_path.clone(), Effect::Delete)],
        Mutation::DeletePaths { rel_paths } => rel_paths
            .iter()
            .map(|p| (p.clone(), Effect::Delete))
            .collect(),
        // A directory prune touches no catalog path effect; it is handled as an
        // always-applied entry in `plan_batch` (never coalesced by path).
        Mutation::RemoveEmptyDir { .. } => vec![],
    }
}

/// Find the staged `source_path` of the highest-seq winning install for `path`.
fn winning_install_source(queued: &[&QueuedMutation], path: &str) -> Option<String> {
    queued
        .iter()
        .filter(|e| {
            matches!(&e.mutation,
            Mutation::InstallFile { rel_path, .. } if rel_path == path)
        })
        .max_by_key(|e| e.seq)
        .and_then(|e| match &e.mutation {
            Mutation::InstallFile { source_path, .. } => Some(source_path.clone()),
            _ => None,
        })
}

/// `path` with a `.tmp` suffix on the file name, for atomic-rename writes.
fn with_tmp_suffix(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".tmp");
    path.with_file_name(name)
}

#[cfg(test)]
#[allow(
    clippy::panic,
    clippy::expect_used,
    clippy::unwrap_used,
    clippy::indexing_slicing
)]
mod tests {
    use super::*;

    fn install(path: &str, src: &str) -> Mutation {
        Mutation::InstallFile {
            rel_path: path.to_owned(),
            source_path: src.to_owned(),
        }
    }

    fn delete(path: &str) -> Mutation {
        Mutation::DeletePath {
            rel_path: path.to_owned(),
        }
    }

    fn remove_dir(path: &str) -> Mutation {
        Mutation::RemoveEmptyDir {
            rel_path: path.to_owned(),
        }
    }

    #[test]
    fn enqueue_assigns_monotonic_ids_and_keeps_queued() {
        let mut q = MutationQueue::default();
        let a = q.enqueue(
            2,
            install("Boombox/a.wav", "/s/a"),
            Some("/s/a".into()),
            None,
        );
        let b = q.enqueue(
            2,
            install("Boombox/b.wav", "/s/b"),
            Some("/s/b".into()),
            None,
        );
        assert_eq!(a.unwrap(), "m-1");
        assert_eq!(b.unwrap(), "m-2");
        assert_eq!(q.live_len(), 2);
        assert!(q.entries().iter().all(|e| e.state == MutationState::Queued));
    }

    #[test]
    fn idempotency_key_dedupes_a_live_entry() {
        let mut q = MutationQueue::default();
        let first = q
            .enqueue(2, install("LockChime.wav", "/s/x"), None, Some("k1".into()))
            .unwrap();
        let again = q
            .enqueue(2, install("LockChime.wav", "/s/x"), None, Some("k1".into()))
            .unwrap();
        assert_eq!(first, again);
        assert_eq!(q.live_len(), 1);
    }

    #[test]
    fn enqueue_refuses_when_full() {
        let mut q = MutationQueue::default();
        for i in 0..MAX_QUEUE_ENTRIES {
            q.enqueue(2, delete(&format!("Music/{i}.mp3")), None, None)
                .unwrap();
        }
        let over = q.enqueue(2, delete("Music/extra.mp3"), None, None);
        assert!(over.is_err());
        assert!(over.unwrap_err().contains("queue full"));
    }

    #[test]
    fn last_writer_wins_for_the_chime_slot() {
        // Two installs to the same path: only the latest survives the plan.
        let mut q = MutationQueue::default();
        q.enqueue(2, install("LockChime.wav", "/s/old"), None, None)
            .unwrap();
        q.enqueue(2, install("LockChime.wav", "/s/new"), None, None)
            .unwrap();
        let plan = q.plan_batch(2);
        assert_eq!(
            plan.applies,
            vec![install("LockChime.wav", "/s/new")],
            "newest install wins"
        );
        assert_eq!(plan.apply_seqs, vec![2]);
        assert_eq!(plan.coalesced_seqs, vec![1]);
    }

    #[test]
    fn delete_after_install_supersedes_the_install() {
        let mut q = MutationQueue::default();
        q.enqueue(2, install("Music/x.mp3", "/s/x"), None, None)
            .unwrap();
        q.enqueue(2, delete("Music/x.mp3"), None, None).unwrap();
        let plan = q.plan_batch(2);
        assert_eq!(
            plan.applies,
            vec![Mutation::DeletePaths {
                rel_paths: vec!["Music/x.mp3".to_owned()]
            }]
        );
        assert_eq!(plan.apply_seqs, vec![2]);
        assert_eq!(plan.coalesced_seqs, vec![1]);
    }

    #[test]
    fn install_after_delete_supersedes_the_delete() {
        let mut q = MutationQueue::default();
        q.enqueue(2, delete("Music/x.mp3"), None, None).unwrap();
        q.enqueue(2, install("Music/x.mp3", "/s/x2"), None, None)
            .unwrap();
        let plan = q.plan_batch(2);
        assert_eq!(plan.applies, vec![install("Music/x.mp3", "/s/x2")]);
        assert_eq!(plan.apply_seqs, vec![2]);
        assert_eq!(plan.coalesced_seqs, vec![1]);
    }

    #[test]
    fn independent_paths_emit_one_delete_apply_per_seq() {
        let mut q = MutationQueue::default();
        q.enqueue(2, delete("Music/a.mp3"), None, None).unwrap();
        q.enqueue(2, delete("Music/b.mp3"), None, None).unwrap();
        q.enqueue(2, install("Boombox/c.wav", "/s/c"), None, None)
            .unwrap();
        let plan = q.plan_batch(2);
        assert_eq!(plan.applies.len(), 3);
        assert!(matches!(
            &plan.applies[0],
            Mutation::DeletePaths { rel_paths } if rel_paths == &vec!["Music/a.mp3".to_owned()]
        ));
        assert!(matches!(
            &plan.applies[1],
            Mutation::DeletePaths { rel_paths } if rel_paths == &vec!["Music/b.mp3".to_owned()]
        ));
        assert_eq!(plan.applies[2], install("Boombox/c.wav", "/s/c"));
        assert_eq!(plan.apply_seqs.len(), 3);
        assert!(plan.coalesced_seqs.is_empty());
    }

    #[test]
    fn apply_seq_groups_stay_parallel_and_cover_apply_seqs() {
        let mut q = MutationQueue::default();
        q.enqueue(2, delete("Music/a.mp3"), None, None).unwrap();
        q.enqueue(2, remove_dir("Music"), None, None).unwrap();
        q.enqueue(2, install("Boombox/c.wav", "/s/c"), None, None)
            .unwrap();

        let plan = q.plan_batch(2);
        assert_eq!(plan.apply_seq_groups.len(), plan.applies.len());

        let grouped: std::collections::BTreeSet<u64> =
            plan.apply_seq_groups.iter().flatten().copied().collect();
        let apply_set: std::collections::BTreeSet<u64> = plan.apply_seqs.iter().copied().collect();
        assert_eq!(grouped, apply_set);
    }

    #[test]
    fn folder_delete_orders_dir_prune_after_files_and_marks_it_applied() {
        let mut q = MutationQueue::default();
        // A folder delete: the child-file deletes plus a prune of the now-empty dir.
        q.enqueue(2, delete("Music/Artist/Album/a.mp3"), None, None)
            .unwrap();
        q.enqueue(2, delete("Music/Artist/Album/b.mp3"), None, None)
            .unwrap();
        q.enqueue(2, remove_dir("Music/Artist/Album"), None, None)
            .unwrap();
        let plan = q.plan_batch(2);

        assert_eq!(plan.applies.len(), 3);
        assert!(matches!(
            &plan.applies[0],
            Mutation::DeletePaths { rel_paths }
                if rel_paths == &vec!["Music/Artist/Album/a.mp3".to_owned()]
        ));
        assert!(matches!(
            &plan.applies[1],
            Mutation::DeletePaths { rel_paths }
                if rel_paths == &vec!["Music/Artist/Album/b.mp3".to_owned()]
        ));
        assert_eq!(plan.applies[2], remove_dir("Music/Artist/Album"));
        // The prune (seq 3) has no path effect but MUST be applied, not coalesced.
        assert!(plan.apply_seqs.contains(&3));
        assert!(plan.coalesced_seqs.is_empty());
    }

    #[test]
    fn folder_delete_dir_prune_alone_is_applied() {
        // Repairing an already-orphaned empty folder: no file deletes, just the prune.
        let mut q = MutationQueue::default();
        q.enqueue(2, remove_dir("Music/EmptyOrphan"), None, None)
            .unwrap();
        let plan = q.plan_batch(2);
        assert_eq!(plan.applies, vec![remove_dir("Music/EmptyOrphan")]);
        assert_eq!(plan.apply_seqs, vec![1]);
        assert!(plan.coalesced_seqs.is_empty());
    }

    #[test]
    fn folder_delete_dedups_repeated_dir_prune() {
        let mut q = MutationQueue::default();
        q.enqueue(2, remove_dir("Music/Dir"), None, None).unwrap();
        q.enqueue(2, remove_dir("Music/Dir"), None, None).unwrap();
        let plan = q.plan_batch(2);
        // Same dir twice → emitted once, but BOTH seqs retired (applied).
        assert_eq!(plan.applies, vec![remove_dir("Music/Dir")]);
        assert_eq!(plan.apply_seqs.len(), 2);
        assert!(plan.coalesced_seqs.is_empty());
    }

    #[test]
    fn plan_is_per_partition() {
        let mut q = MutationQueue::default();
        q.enqueue(1, delete("RecentClips/x"), None, None).unwrap();
        q.enqueue(2, delete("Music/y.mp3"), None, None).unwrap();
        assert_eq!(q.pending_partitions(), vec![1, 2]);
        let p2 = q.plan_batch(2);
        assert_eq!(
            p2.applies,
            vec![Mutation::DeletePaths {
                rel_paths: vec!["Music/y.mp3".to_owned()]
            }]
        );
        assert_eq!(p2.apply_seqs, vec![2]);
    }

    #[test]
    fn set_state_and_reclaim_blobs_then_prune() {
        let mut q = MutationQueue::default();
        q.enqueue(
            2,
            install("LockChime.wav", "/s/x"),
            Some("/s/x".into()),
            None,
        )
        .unwrap();
        q.set_state(&[1], MutationState::Applied);
        assert_eq!(q.reclaimable_blobs(&[1]), vec!["/s/x".to_owned()]);
        q.prune_terminal();
        assert_eq!(q.entries().len(), 0);
    }

    #[test]
    fn persist_then_load_roundtrips_through_the_journal() {
        let dir = std::env::temp_dir().join(format!("gqtest-{}", now_ms()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("queue.json");
        let mut q = MutationQueue::default();
        q.enqueue(
            2,
            install("LockChime.wav", "/s/x"),
            Some("/s/x".into()),
            Some("k".into()),
        )
        .unwrap();
        q.persist(&path).unwrap();

        let loaded = MutationQueue::load(&path);
        assert_eq!(loaded.live_len(), 1);
        let e = &loaded.entries()[0];
        assert_eq!(e.id, "m-1");
        assert_eq!(e.idempotency_key.as_deref(), Some("k"));
        // next_seq survives so ids never collide after a restart.
        let mut loaded = loaded;
        let next = loaded
            .enqueue(2, delete("Music/z.mp3"), None, None)
            .unwrap();
        assert_eq!(next, "m-2");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn load_missing_journal_is_empty() {
        let path = std::env::temp_dir().join("gqtest-does-not-exist-xyz.json");
        let q = MutationQueue::load(&path);
        assert_eq!(q.live_len(), 0);
    }

    #[test]
    fn each_delete_seq_becomes_its_own_apply() {
        let mut q = MutationQueue::default();
        let total = MAX_DELETE_PATHS + 5;
        for i in 0..total {
            q.enqueue(2, delete(&format!("Music/{i:03}.mp3")), None, None)
                .unwrap();
        }
        let plan = q.plan_batch(2);
        assert_eq!(plan.applies.len(), total);
        let mut seen = Vec::new();
        for m in &plan.applies {
            match m {
                Mutation::DeletePaths { rel_paths } => {
                    assert_eq!(rel_paths.len(), 1);
                    seen.extend(rel_paths.iter().cloned());
                }
                other => panic!("expected DeletePaths, got {other:?}"),
            }
        }
        assert_eq!(seen.len(), total);
        assert_eq!(plan.apply_seqs.len(), total);
        assert_eq!(plan.apply_seq_groups.len(), total);
        assert!(plan.apply_seq_groups.iter().all(|group| group.len() == 1));
    }

    #[test]
    fn delete_seq_groups_do_not_span_delete_chunks() {
        let mut q = MutationQueue::default();
        let seq1_paths: Vec<String> = (0..10).map(|i| format!("Music/s1-{i:02}.mp3")).collect();
        let seq2_paths: Vec<String> = (0..10).map(|i| format!("Music/s2-{i:02}.mp3")).collect();
        q.enqueue(
            2,
            Mutation::DeletePaths {
                rel_paths: seq1_paths.clone(),
            },
            None,
            None,
        )
        .unwrap();
        q.enqueue(
            2,
            Mutation::DeletePaths {
                rel_paths: seq2_paths.clone(),
            },
            None,
            None,
        )
        .unwrap();

        let plan = q.plan_batch(2);
        assert_eq!(plan.applies.len(), 2);

        let mut chunk_seq_sets: Vec<std::collections::BTreeSet<u64>> = Vec::new();
        for (mutation, group) in plan.applies.iter().zip(plan.apply_seq_groups.iter()) {
            assert_eq!(group.len(), 1, "one delete apply per winning seq");
            let rel_paths = match mutation {
                Mutation::DeletePaths { rel_paths } => rel_paths,
                other => panic!("expected DeletePaths, got {other:?}"),
            };
            let seqs = rel_paths
                .iter()
                .map(|path| {
                    if seq1_paths.contains(path) {
                        1u64
                    } else if seq2_paths.contains(path) {
                        2u64
                    } else {
                        panic!("unexpected delete path in chunk: {path}");
                    }
                })
                .collect();
            assert_eq!(seqs, group.iter().copied().collect());
            chunk_seq_sets.push(seqs);
        }

        for (idx, seqs) in chunk_seq_sets.iter().enumerate() {
            for other in chunk_seq_sets.iter().skip(idx + 1) {
                assert!(
                    seqs.is_disjoint(other),
                    "each delete chunk must contain a disjoint set of contributing seqs"
                );
            }
        }

        let expected_per_seq: std::collections::BTreeMap<u64, std::collections::BTreeSet<String>> =
            [
                (1u64, seq1_paths.iter().cloned().collect()),
                (2u64, seq2_paths.iter().cloned().collect()),
            ]
            .into_iter()
            .collect();
        for (seq, expected_paths) in &expected_per_seq {
            let containing_chunks: Vec<std::collections::BTreeSet<String>> = plan
                .applies
                .iter()
                .filter_map(|mutation| match mutation {
                    Mutation::DeletePaths { rel_paths } => {
                        let chunk_paths: std::collections::BTreeSet<String> =
                            rel_paths.iter().cloned().collect();
                        if chunk_paths.iter().any(|p| expected_paths.contains(p)) {
                            Some(chunk_paths)
                        } else {
                            None
                        }
                    }
                    _ => None,
                })
                .collect();
            assert_eq!(
                containing_chunks.len(),
                1,
                "winning paths for seq {seq} must appear in exactly one chunk"
            );
            let only_chunk = containing_chunks.first().expect("one containing chunk");
            assert!(
                expected_paths.is_subset(only_chunk),
                "all winning paths for seq {seq} must stay together in one chunk"
            );
        }
    }

    #[test]
    fn requeue_inflight_resets_interrupted_applies() {
        let mut q = MutationQueue::default();
        q.enqueue(
            2,
            install("LockChime.wav", "/s/x"),
            Some("/s/x".into()),
            None,
        )
        .unwrap();
        q.enqueue(2, delete("Music/y.mp3"), None, None).unwrap();
        q.set_state(&[1, 2], MutationState::Applying);
        assert_eq!(q.requeue_inflight(), 2);
        assert!(q.entries().iter().all(|e| e.state == MutationState::Queued));
        // Idempotent: nothing left applying on a second call.
        assert_eq!(q.requeue_inflight(), 0);
    }

    #[test]
    fn requeue_inflight_leaves_terminal_entries_alone() {
        let mut q = MutationQueue::default();
        q.enqueue(2, delete("Music/a.mp3"), None, None).unwrap();
        q.enqueue(2, delete("Music/b.mp3"), None, None).unwrap();
        q.set_state(&[1], MutationState::Applied);
        q.set_state(&[2], MutationState::Applying);
        assert_eq!(q.requeue_inflight(), 1);
        assert_eq!(q.find("m-1").unwrap().state, MutationState::Applied);
        assert_eq!(q.find("m-2").unwrap().state, MutationState::Queued);
    }
}
