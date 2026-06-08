//! indexd-mediated mutation entry points for durable control state:
//! leases (D3), `archive_items.delete_state` transitions, the `durable`
//! flag, and the WAL checkpoint/truncate hook.
//!
//! `indexd` is the **sole `SQLite` writer** (D1 §1, D3 §2): `webd`,
//! `uploadd` and `retentiond` never write these rows; they call these
//! entry points (later over the UDS RPC; the transport is OQ-2 and out of
//! scope for this lane). This module owns the *table mutations*; the full
//! governor/holder protocol lives in the consuming services.
//!
//! ## Boot-scoped monotonic deadlines (D3 §2.2, §4.2)
//!
//! The Pi has **no RTC**, so lease deadlines are **monotonic**
//! milliseconds within a single `indexd` boot, never wall-clock. Each boot
//! mints a fresh `boot_id`; every lease from a prior `boot_id` is stale by
//! definition and reaped at startup. A wall-clock jump can therefore
//! neither pin a dead lease forever nor reap a live one. [`BootContext`]
//! holds the `boot_id` and the monotonic anchor; the free functions take
//! an explicit `mono_now_ms` so the logic is host-testable without sleeps.

use std::time::Instant;

use rusqlite::{Connection, OptionalExtension, params};

use crate::db::{DbError, now_epoch_s};

/// Lease kind (`leases.kind` CHECK constraint).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LeaseKind {
    /// Held by `uploadd` while a transfer runs.
    Upload,
    /// Held by `webd` while a stream/export is in flight.
    Playback,
}

impl LeaseKind {
    /// The D1 `leases.kind` string.
    #[must_use]
    pub fn as_db_str(self) -> &'static str {
        match self {
            Self::Upload => "upload",
            Self::Playback => "playback",
        }
    }
}

/// Result of an `acquire`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LeaseGrant {
    /// Lease granted.
    Granted {
        /// New `leases.id`.
        lease_id: i64,
        /// 128-bit generation token (hex) presented on renew/release.
        generation: String,
        /// Boot-scoped monotonic deadline (ms).
        expires_mono_ms: i64,
    },
    /// Lease refused (item missing or already `DELETE_CLAIMED`+).
    Denied {
        /// Human reason for diagnostics.
        reason: String,
    },
}

/// One granted lease within an `acquire_for_clip` result.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClipLease {
    /// The backing archive item.
    pub archive_item_id: i64,
    /// New `leases.id`.
    pub lease_id: i64,
    /// Generation token (hex).
    pub generation: String,
    /// Boot-scoped monotonic deadline (ms).
    pub expires_mono_ms: i64,
}

/// Result of an `acquire_for_clip` (all-or-nothing).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClipLeaseGrant {
    /// Every backing archive item was leased.
    Granted {
        /// One lease per backing archive item.
        leases: Vec<ClipLease>,
    },
    /// At least one backing item was unclaimable; nothing was granted.
    Denied {
        /// Human reason for diagnostics.
        reason: String,
    },
}

/// Result of a `renew`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RenewResult {
    /// Deadline extended.
    Renewed {
        /// New boot-scoped monotonic deadline (ms).
        expires_mono_ms: i64,
    },
    /// Refused: gen mismatch, past deadline, or subject not `LIVE`.
    Stale {
        /// Human reason for diagnostics.
        reason: String,
    },
}

/// Result of a `release`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReleaseResult {
    /// A matching lease row was removed.
    Released,
    /// No row matched (already released/reaped) — a no-op.
    NoOp,
}

/// Generates dependency-free 128-bit hex tokens for `boot_id` and lease /
/// delete generations. The lease protocol needs **uniqueness and
/// monotonicity** (defeating replay from a crashed-then-restarted holder),
/// not cryptographic unpredictability, so a time+counter-seeded splitmix64
/// stream suffices. No `rand`/`getrandom` is available in this workspace.
mod token {
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    fn splitmix64(seed: u64) -> u64 {
        let mut z = seed.wrapping_add(0x9E37_79B9_7F4A_7C15);
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// A fresh 128-bit token as a 32-char lowercase hex string.
    pub fn token_128() -> String {
        // Truncating u128 nanos to u64 is intentional — we only need a
        // well-mixed seed, not the full range.
        #[allow(clippy::cast_possible_truncation)]
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0_u64, |d| d.as_nanos() as u64);
        let counter = COUNTER.fetch_add(1, Ordering::Relaxed);
        // ASLR'd stack address adds a little per-process entropy.
        let stack = std::ptr::addr_of!(nanos) as u64;
        let hi = splitmix64(nanos ^ stack ^ counter.rotate_left(32));
        let lo = splitmix64(counter ^ nanos.rotate_left(17) ^ stack.rotate_left(40));
        format!("{hi:016x}{lo:016x}")
    }
}

/// Per-boot lease context: the minted `boot_id` and the monotonic anchor.
/// Created once at `indexd` startup.
#[derive(Debug)]
pub struct BootContext {
    boot_id: String,
    anchor: Instant,
}

impl Default for BootContext {
    fn default() -> Self {
        Self::new()
    }
}

impl BootContext {
    /// Mint a fresh boot context (new `boot_id`, monotonic anchor = now).
    #[must_use]
    pub fn new() -> Self {
        Self {
            boot_id: token::token_128(),
            anchor: Instant::now(),
        }
    }

    /// This boot's id.
    #[must_use]
    pub fn boot_id(&self) -> &str {
        &self.boot_id
    }

    /// Monotonic milliseconds since this boot context was created.
    #[must_use]
    pub fn mono_now_ms(&self) -> i64 {
        i64::try_from(self.anchor.elapsed().as_millis()).unwrap_or(i64::MAX)
    }

    /// Reap stale leases for this boot (see [`reap_stale_leases`]).
    ///
    /// # Errors
    /// Returns [`DbError`] on failure.
    pub fn reap(&self, conn: &Connection) -> Result<usize, DbError> {
        reap_stale_leases(conn, &self.boot_id, self.mono_now_ms())
    }

    /// Acquire a lease on one archive item (see [`lease_acquire`]).
    ///
    /// # Errors
    /// Returns [`DbError`] on failure.
    pub fn acquire(
        &self,
        conn: &Connection,
        archive_item_id: i64,
        kind: LeaseKind,
        holder: &str,
        ttl_s: u32,
    ) -> Result<LeaseGrant, DbError> {
        lease_acquire(
            conn,
            &self.boot_id,
            self.mono_now_ms(),
            archive_item_id,
            kind,
            holder,
            ttl_s,
        )
    }

    /// Acquire leases on every archive item backing a clip, atomically
    /// (see [`lease_acquire_for_clip`]).
    ///
    /// # Errors
    /// Returns [`DbError`] on failure.
    pub fn acquire_for_clip(
        &self,
        conn: &mut Connection,
        clip_id: i64,
        kind: LeaseKind,
        holder: &str,
        ttl_s: u32,
    ) -> Result<ClipLeaseGrant, DbError> {
        lease_acquire_for_clip(
            conn,
            &self.boot_id,
            self.mono_now_ms(),
            clip_id,
            kind,
            holder,
            ttl_s,
        )
    }

    /// Renew a lease (see [`lease_renew`]).
    ///
    /// # Errors
    /// Returns [`DbError`] on failure.
    pub fn renew(
        &self,
        conn: &Connection,
        lease_id: i64,
        generation: &str,
        ttl_s: u32,
    ) -> Result<RenewResult, DbError> {
        lease_renew(
            conn,
            &self.boot_id,
            self.mono_now_ms(),
            lease_id,
            generation,
            ttl_s,
        )
    }

    /// Claim an archive item for deletion, gated on leases
    /// (see [`claim_for_delete`]).
    ///
    /// # Errors
    /// Returns [`DbError`] on failure.
    pub fn claim_for_delete(
        &self,
        conn: &mut Connection,
        archive_item_id: i64,
    ) -> Result<Option<String>, DbError> {
        claim_for_delete(conn, &self.boot_id, self.mono_now_ms(), archive_item_id)
    }
}

/// Acquire a lease on one archive item. Granted only if the item exists
/// and is `LIVE`; otherwise `Denied` (D3 §2.1, §3).
///
/// # Errors
///
/// Returns [`DbError`] if a statement fails.
pub fn lease_acquire(
    conn: &Connection,
    boot_id: &str,
    mono_now_ms: i64,
    archive_item_id: i64,
    kind: LeaseKind,
    holder: &str,
    ttl_s: u32,
) -> Result<LeaseGrant, DbError> {
    let state: Option<String> = conn
        .query_row(
            "SELECT delete_state FROM archive_items WHERE id = ?1",
            params![archive_item_id],
            |r| r.get(0),
        )
        .optional()?;
    match state.as_deref() {
        None => {
            return Ok(LeaseGrant::Denied {
                reason: format!("archive_item {archive_item_id} does not exist"),
            });
        }
        Some("LIVE") => {}
        Some(other) => {
            return Ok(LeaseGrant::Denied {
                reason: format!("archive_item {archive_item_id} is {other}, not LIVE"),
            });
        }
    }
    let generation = token::token_128();
    let expires_mono_ms = mono_now_ms.saturating_add(i64::from(ttl_s).saturating_mul(1000));
    conn.execute(
        "INSERT INTO leases
             (archive_item_id, kind, holder, gen, boot_id, acquired_wall,
              expires_mono_ms, preempt_req)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 0)",
        params![
            archive_item_id,
            kind.as_db_str(),
            holder,
            generation,
            boot_id,
            now_epoch_s(),
            expires_mono_ms,
        ],
    )?;
    Ok(LeaseGrant::Granted {
        lease_id: conn.last_insert_rowid(),
        generation,
        expires_mono_ms,
    })
}

/// Acquire leases on every archive item backing `clip_id`, all-or-nothing
/// (D3 §2.1). If the clip has no backing archive items, or any backing
/// item is not `LIVE`, nothing is granted.
///
/// # Errors
///
/// Returns [`DbError`] if a statement fails.
pub fn lease_acquire_for_clip(
    conn: &mut Connection,
    boot_id: &str,
    mono_now_ms: i64,
    clip_id: i64,
    kind: LeaseKind,
    holder: &str,
    ttl_s: u32,
) -> Result<ClipLeaseGrant, DbError> {
    let item_ids: Vec<i64> = {
        let mut stmt = conn.prepare(
            "SELECT archive_item_id FROM archive_item_clips WHERE clip_id = ?1
             ORDER BY archive_item_id ASC",
        )?;
        let rows = stmt.query_map(params![clip_id], |r| r.get::<_, i64>(0))?;
        let mut ids = Vec::new();
        for row in rows {
            ids.push(row?);
        }
        ids
    };
    if item_ids.is_empty() {
        return Ok(ClipLeaseGrant::Denied {
            reason: format!("clip {clip_id} has no backing archive_items"),
        });
    }

    let tx = conn.transaction()?;
    let mut leases = Vec::with_capacity(item_ids.len());
    for item_id in item_ids {
        match lease_acquire(&tx, boot_id, mono_now_ms, item_id, kind, holder, ttl_s)? {
            LeaseGrant::Granted {
                lease_id,
                generation,
                expires_mono_ms,
            } => leases.push(ClipLease {
                archive_item_id: item_id,
                lease_id,
                generation,
                expires_mono_ms,
            }),
            LeaseGrant::Denied { reason } => {
                // Atomic: drop the whole transaction, grant nothing.
                drop(tx);
                return Ok(ClipLeaseGrant::Denied { reason });
            }
        }
    }
    tx.commit()?;
    Ok(ClipLeaseGrant::Granted { leases })
}

/// Renew a lease. Returns `Stale` unless ALL hold (D3 §2.2): the
/// `lease_id`+`gen` match a row of the **current boot**, the lease is not
/// past its deadline, and the subject archive item is still `LIVE`.
///
/// # Errors
///
/// Returns [`DbError`] if a statement fails.
pub fn lease_renew(
    conn: &Connection,
    boot_id: &str,
    mono_now_ms: i64,
    lease_id: i64,
    generation: &str,
    ttl_s: u32,
) -> Result<RenewResult, DbError> {
    let row: Option<(String, String, i64, i64)> = conn
        .query_row(
            "SELECT gen, boot_id, expires_mono_ms, archive_item_id
               FROM leases WHERE id = ?1",
            params![lease_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    let Some((row_gen, row_boot, expires, archive_item_id)) = row else {
        return Ok(RenewResult::Stale {
            reason: "lease does not exist".to_owned(),
        });
    };
    if row_gen != generation || row_boot != boot_id {
        return Ok(RenewResult::Stale {
            reason: "gen/boot mismatch".to_owned(),
        });
    }
    if expires <= mono_now_ms {
        return Ok(RenewResult::Stale {
            reason: "lease past deadline".to_owned(),
        });
    }
    let subject_live: bool = conn
        .query_row(
            "SELECT delete_state = 'LIVE' FROM archive_items WHERE id = ?1",
            params![archive_item_id],
            |r| r.get(0),
        )
        .optional()?
        .unwrap_or(false);
    if !subject_live {
        return Ok(RenewResult::Stale {
            reason: "subject not LIVE".to_owned(),
        });
    }
    let expires_mono_ms = mono_now_ms.saturating_add(i64::from(ttl_s).saturating_mul(1000));
    conn.execute(
        "UPDATE leases SET expires_mono_ms = ?2 WHERE id = ?1",
        params![lease_id, expires_mono_ms],
    )?;
    Ok(RenewResult::Renewed { expires_mono_ms })
}

/// Release a lease. Idempotent: a non-matching `lease_id`/`gen` is a
/// `NoOp` (D3 §2.1).
///
/// # Errors
///
/// Returns [`DbError`] if a statement fails.
pub fn lease_release(
    conn: &Connection,
    lease_id: i64,
    generation: &str,
) -> Result<ReleaseResult, DbError> {
    let changed = conn.execute(
        "DELETE FROM leases WHERE id = ?1 AND gen = ?2",
        params![lease_id, generation],
    )?;
    Ok(if changed > 0 {
        ReleaseResult::Released
    } else {
        ReleaseResult::NoOp
    })
}

/// Reap stale leases: every lease from a prior boot (unconditionally) plus
/// every lease past its monotonic deadline within this boot (D3 §4.2).
/// Returns the number reaped. Run at startup and opportunistically.
///
/// # Errors
///
/// Returns [`DbError`] if the statement fails.
pub fn reap_stale_leases(
    conn: &Connection,
    boot_id: &str,
    mono_now_ms: i64,
) -> Result<usize, DbError> {
    let changed = conn.execute(
        "DELETE FROM leases
          WHERE boot_id <> ?1
             OR expires_mono_ms <= ?2",
        params![boot_id, mono_now_ms],
    )?;
    Ok(changed)
}

/// Whether an archive item currently has an **unexpired** lease of this
/// boot (D3 §3): `boot_id == current && expires_mono_ms > mono_now`.
///
/// # Errors
///
/// Returns [`DbError`] if the query fails.
pub fn has_unexpired_lease(
    conn: &Connection,
    boot_id: &str,
    mono_now_ms: i64,
    archive_item_id: i64,
) -> Result<bool, DbError> {
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM leases
          WHERE archive_item_id = ?1 AND boot_id = ?2 AND expires_mono_ms > ?3",
        params![archive_item_id, boot_id, mono_now_ms],
        |r| r.get(0),
    )?;
    Ok(count > 0)
}

/// Atomically claim an archive item for deletion (D3 §3): aborts if the
/// item has any unexpired lease or is not `LIVE`. On success transitions
/// `LIVE → DELETE_CLAIMED`, records a fresh random `delete_gen` (the trash
/// token; never wall-clock — the Pi has no RTC, D3 §4), and returns it.
/// Returns `None` if the claim was refused.
///
/// # Errors
///
/// Returns [`DbError`] if a statement fails.
pub fn claim_for_delete(
    conn: &mut Connection,
    boot_id: &str,
    mono_now_ms: i64,
    archive_item_id: i64,
) -> Result<Option<String>, DbError> {
    let tx = conn.transaction()?;
    if has_unexpired_lease(&tx, boot_id, mono_now_ms, archive_item_id)? {
        drop(tx);
        return Ok(None);
    }
    let is_live: bool = tx
        .query_row(
            "SELECT delete_state = 'LIVE' FROM archive_items WHERE id = ?1",
            params![archive_item_id],
            |r| r.get(0),
        )
        .optional()?
        .unwrap_or(false);
    if !is_live {
        drop(tx);
        return Ok(None);
    }
    let generation = token::token_128();
    tx.execute(
        "UPDATE archive_items
            SET delete_state = 'DELETE_CLAIMED', delete_gen = ?2, updated_at = ?3
          WHERE id = ?1",
        params![archive_item_id, generation, now_epoch_s()],
    )?;
    tx.commit()?;
    Ok(Some(generation))
}

/// Idempotently set an archive item's `delete_state`. Used by the
/// `retentiond` single-deleter finishers and the startup recovery matrix
/// (D3 §4, §4.1); each is safe to re-apply after a crash.
///
/// # Errors
///
/// Returns [`DbError`] if the statement fails.
fn set_delete_state(
    conn: &Connection,
    archive_item_id: i64,
    state: &str,
    bytes_freed: Option<i64>,
) -> Result<(), DbError> {
    conn.execute(
        "UPDATE archive_items
            SET delete_state = ?2,
                bytes_freed  = COALESCE(?3, bytes_freed),
                updated_at   = ?4
          WHERE id = ?1",
        params![archive_item_id, state, bytes_freed, now_epoch_s()],
    )?;
    Ok(())
}

/// `DELETE_CLAIMED → DELETING` (D3 §4 step 4).
///
/// # Errors
/// Returns [`DbError`] on failure.
pub fn mark_deleting(conn: &Connection, archive_item_id: i64) -> Result<(), DbError> {
    set_delete_state(conn, archive_item_id, "DELETING", None)
}

/// `DELETING → DELETED(bytes_freed)` (D3 §4 step 6).
///
/// # Errors
/// Returns [`DbError`] on failure.
pub fn mark_deleted(
    conn: &Connection,
    archive_item_id: i64,
    bytes_freed: i64,
) -> Result<(), DbError> {
    set_delete_state(conn, archive_item_id, "DELETED", Some(bytes_freed))
}

/// Release a delete claim back to `LIVE` (D3 §4.1 recovery).
///
/// # Errors
/// Returns [`DbError`] on failure.
pub fn release_delete_claim(conn: &Connection, archive_item_id: i64) -> Result<(), DbError> {
    set_delete_state(conn, archive_item_id, "LIVE", None)
}

/// Mark a delete attempt failed (`DELETE_FAILED`).
///
/// # Errors
/// Returns [`DbError`] on failure.
pub fn mark_delete_failed(conn: &Connection, archive_item_id: i64) -> Result<(), DbError> {
    set_delete_state(conn, archive_item_id, "DELETE_FAILED", None)
}

/// Quarantine an archive item for investigation (D3 §4.1 anomalies). The
/// `reason` is for the caller's log; D1 has no column for it.
///
/// # Errors
/// Returns [`DbError`] on failure.
pub fn quarantine(conn: &Connection, archive_item_id: i64, reason: &str) -> Result<(), DbError> {
    let _ = reason;
    set_delete_state(conn, archive_item_id, "QUARANTINED", None)
}

/// Set the `durable` flag on an archive item (D3 §6: `uploadd` calls this
/// on a verified upload; only then may `retentiond` treat the local copy
/// as safe to evict). Deletion remains `retentiond`'s alone.
///
/// # Errors
///
/// Returns [`DbError`] if the statement fails.
pub fn set_durable(conn: &Connection, archive_item_id: i64, durable: bool) -> Result<(), DbError> {
    conn.execute(
        "UPDATE archive_items SET durable = ?2, updated_at = ?3 WHERE id = ?1",
        params![archive_item_id, i64::from(durable), now_epoch_s()],
    )?;
    Ok(())
}

/// Run `PRAGMA wal_checkpoint(TRUNCATE)` and return the `SQLite` triple
/// `(busy, log_frames, checkpointed_frames)`. The entry point
/// `retentiond` calls to bound WAL growth (storage.md §5.2). `busy == 1`
/// means a reader held the checkpoint back; the WAL was not fully
/// truncated and the caller may retry.
///
/// # Errors
///
/// Returns [`DbError`] if the pragma fails.
pub fn wal_checkpoint_truncate(conn: &Connection) -> Result<(i64, i64, i64), DbError> {
    let triple = conn.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| {
        Ok((r.get(0)?, r.get(1)?, r.get(2)?))
    })?;
    Ok(triple)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::indexing_slicing, clippy::panic)]

    use rusqlite::{Connection, params};

    use super::{
        ClipLeaseGrant, LeaseGrant, LeaseKind, ReleaseResult, RenewResult, claim_for_delete,
        has_unexpired_lease, lease_acquire, lease_acquire_for_clip, lease_release, lease_renew,
        mark_deleted, mark_deleting, reap_stale_leases, set_durable, wal_checkpoint_truncate,
    };
    use crate::db::open_in_memory;

    const BOOT: &str = "boot-current";
    const TTL: u32 = 60;

    fn insert_archive_item(conn: &Connection, path: &str) -> i64 {
        conn.execute(
            "INSERT INTO archive_items (folder_class, path, archived_at, created_at, updated_at)
             VALUES ('SavedClips', ?1, 0, 0, 0)",
            params![path],
        )
        .unwrap();
        conn.last_insert_rowid()
    }

    fn insert_clip(conn: &Connection, key: &str) -> i64 {
        conn.execute(
            "INSERT INTO clips (canonical_key, started_at, partition, folder_class, created_at, updated_at)
             VALUES (?1, 0, 'p', 'SavedClips', 0, 0)",
            params![key],
        )
        .unwrap();
        conn.last_insert_rowid()
    }

    #[test]
    fn acquire_grants_on_live_and_denies_on_claimed() {
        let conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "/a");
        let grant =
            lease_acquire(&conn, BOOT, 0, item, LeaseKind::Playback, "webd:1", TTL).unwrap();
        let LeaseGrant::Granted {
            expires_mono_ms, ..
        } = grant
        else {
            panic!("expected Granted, got {grant:?}");
        };
        assert_eq!(expires_mono_ms, 60_000);

        // Claim it for delete -> a new acquire must be Denied.
        conn.execute(
            "UPDATE archive_items SET delete_state = 'DELETE_CLAIMED' WHERE id = ?1",
            params![item],
        )
        .unwrap();
        let denied =
            lease_acquire(&conn, BOOT, 0, item, LeaseKind::Upload, "uploadd", TTL).unwrap();
        assert!(matches!(denied, LeaseGrant::Denied { .. }));
    }

    #[test]
    fn acquire_denies_missing_item() {
        let conn = open_in_memory().unwrap();
        let denied = lease_acquire(&conn, BOOT, 0, 999, LeaseKind::Upload, "uploadd", TTL).unwrap();
        assert!(matches!(denied, LeaseGrant::Denied { .. }));
    }

    #[test]
    fn renew_rules() {
        let conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "/a");
        let LeaseGrant::Granted {
            lease_id,
            generation,
            ..
        } = lease_acquire(&conn, BOOT, 0, item, LeaseKind::Playback, "webd", TTL).unwrap()
        else {
            panic!("expected Granted");
        };

        // Wrong gen -> Stale.
        assert!(matches!(
            lease_renew(&conn, BOOT, 1000, lease_id, "deadbeef", TTL).unwrap(),
            RenewResult::Stale { .. }
        ));
        // Correct gen, within deadline -> Renewed with extended deadline.
        let RenewResult::Renewed { expires_mono_ms } =
            lease_renew(&conn, BOOT, 1000, lease_id, &generation, TTL).unwrap()
        else {
            panic!("expected Renewed");
        };
        assert_eq!(expires_mono_ms, 61_000);
        // Past deadline -> Stale (resurrection race closed).
        assert!(matches!(
            lease_renew(&conn, BOOT, 200_000, lease_id, &generation, TTL).unwrap(),
            RenewResult::Stale { .. }
        ));
        // Wrong boot -> Stale.
        assert!(matches!(
            lease_renew(&conn, "other-boot", 1000, lease_id, &generation, TTL).unwrap(),
            RenewResult::Stale { .. }
        ));
    }

    #[test]
    fn renew_stale_when_subject_not_live() {
        let conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "/a");
        let LeaseGrant::Granted {
            lease_id,
            generation,
            ..
        } = lease_acquire(&conn, BOOT, 0, item, LeaseKind::Playback, "webd", TTL).unwrap()
        else {
            panic!("expected Granted");
        };
        conn.execute(
            "UPDATE archive_items SET delete_state = 'DELETING' WHERE id = ?1",
            params![item],
        )
        .unwrap();
        assert!(matches!(
            lease_renew(&conn, BOOT, 1000, lease_id, &generation, TTL).unwrap(),
            RenewResult::Stale { .. }
        ));
    }

    #[test]
    fn release_then_noop() {
        let conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "/a");
        let LeaseGrant::Granted {
            lease_id,
            generation,
            ..
        } = lease_acquire(&conn, BOOT, 0, item, LeaseKind::Upload, "uploadd", TTL).unwrap()
        else {
            panic!("expected Granted");
        };
        assert_eq!(
            lease_release(&conn, lease_id, &generation).unwrap(),
            ReleaseResult::Released
        );
        assert_eq!(
            lease_release(&conn, lease_id, &generation).unwrap(),
            ReleaseResult::NoOp
        );
    }

    #[test]
    fn reap_removes_prior_boot_and_expired() {
        let conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "/a");
        // Current-boot, unexpired.
        lease_acquire(&conn, BOOT, 0, item, LeaseKind::Playback, "webd", TTL).unwrap();
        // Prior-boot lease (raw insert).
        conn.execute(
            "INSERT INTO leases (archive_item_id, kind, holder, gen, boot_id, expires_mono_ms)
             VALUES (?1, 'upload', 'old', 'g1', 'prior-boot', 999999)",
            params![item],
        )
        .unwrap();
        // Current-boot, already expired.
        conn.execute(
            "INSERT INTO leases (archive_item_id, kind, holder, gen, boot_id, expires_mono_ms)
             VALUES (?1, 'upload', 'old', 'g2', ?2, 500)",
            params![item, BOOT],
        )
        .unwrap();

        let reaped = reap_stale_leases(&conn, BOOT, 1000).unwrap();
        assert_eq!(reaped, 2);
        let remaining: i64 = conn
            .query_row("SELECT COUNT(*) FROM leases", [], |r| r.get(0))
            .unwrap();
        assert_eq!(remaining, 1);
    }

    #[test]
    fn claim_for_delete_blocked_by_lease_then_succeeds() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "/a");
        lease_acquire(&conn, BOOT, 0, item, LeaseKind::Playback, "webd", TTL).unwrap();

        // Unexpired lease blocks the claim.
        assert!(has_unexpired_lease(&conn, BOOT, 1000, item).unwrap());
        assert_eq!(claim_for_delete(&mut conn, BOOT, 1000, item).unwrap(), None);

        // After the lease lapses, the claim succeeds and a new acquire is denied.
        let generation = claim_for_delete(&mut conn, BOOT, 200_000, item).unwrap();
        assert!(generation.is_some());
        let denied =
            lease_acquire(&conn, BOOT, 200_000, item, LeaseKind::Playback, "webd", TTL).unwrap();
        assert!(matches!(denied, LeaseGrant::Denied { .. }));
    }

    #[test]
    fn delete_state_finishers_and_durable() {
        let conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "/a");
        mark_deleting(&conn, item).unwrap();
        mark_deleted(&conn, item, 4096).unwrap();
        let (state, bytes): (String, i64) = conn
            .query_row(
                "SELECT delete_state, bytes_freed FROM archive_items WHERE id = ?1",
                params![item],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(state, "DELETED");
        assert_eq!(bytes, 4096);
        // Idempotent re-apply.
        mark_deleted(&conn, item, 4096).unwrap();

        set_durable(&conn, item, true).unwrap();
        let durable: i64 = conn
            .query_row(
                "SELECT durable FROM archive_items WHERE id = ?1",
                params![item],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(durable, 1);
    }

    #[test]
    fn acquire_for_clip_is_atomic() {
        let mut conn = open_in_memory().unwrap();
        let clip = insert_clip(&conn, "k1");
        let a1 = insert_archive_item(&conn, "/a1");
        let a2 = insert_archive_item(&conn, "/a2");
        for a in [a1, a2] {
            conn.execute(
                "INSERT INTO archive_item_clips (archive_item_id, clip_id) VALUES (?1, ?2)",
                params![a, clip],
            )
            .unwrap();
        }
        // Both LIVE -> both leased.
        let grant =
            lease_acquire_for_clip(&mut conn, BOOT, 0, clip, LeaseKind::Playback, "webd", TTL)
                .unwrap();
        let ClipLeaseGrant::Granted { leases } = grant else {
            panic!("expected Granted");
        };
        assert_eq!(leases.len(), 2);

        // Make one backing item unclaimable -> next acquire grants nothing.
        conn.execute("DELETE FROM leases", []).unwrap();
        conn.execute(
            "UPDATE archive_items SET delete_state = 'DELETE_CLAIMED' WHERE id = ?1",
            params![a2],
        )
        .unwrap();
        let grant =
            lease_acquire_for_clip(&mut conn, BOOT, 0, clip, LeaseKind::Upload, "uploadd", TTL)
                .unwrap();
        assert!(matches!(grant, ClipLeaseGrant::Denied { .. }));
        let leases_now: i64 = conn
            .query_row("SELECT COUNT(*) FROM leases", [], |r| r.get(0))
            .unwrap();
        assert_eq!(leases_now, 0);
    }

    #[test]
    fn wal_checkpoint_runs() {
        let conn = open_in_memory().unwrap();
        // In-memory degrades WAL to 'memory'; the pragma must still return.
        let _ = wal_checkpoint_truncate(&conn).unwrap();
    }
}
