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

use crate::db::{DbError, PROVEN_DURABLE_PROOF_SQL, now_epoch_s};

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

/// Generates dependency-free 128-bit hex tokens for `boot_id` and lease
/// generations. The lease protocol needs **uniqueness and monotonicity**
/// (defeating replay from a crashed-then-restarted holder), not cryptographic
/// unpredictability, so a time+counter-seeded splitmix64 stream suffices.
///
/// Delete generations are intentionally separate: they become trash path
/// material and therefore must come from OS entropy.
mod token {
    #[cfg(test)]
    use std::cell::Cell;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    #[cfg(test)]
    thread_local! {
        static FORCE_DELETE_GEN_ENTROPY_FAILURE: Cell<bool> = const { Cell::new(false) };
    }

    #[cfg(test)]
    pub struct ScopedDeleteGenEntropyFailure;

    #[cfg(test)]
    impl Drop for ScopedDeleteGenEntropyFailure {
        fn drop(&mut self) {
            FORCE_DELETE_GEN_ENTROPY_FAILURE.with(|flag| flag.set(false));
        }
    }

    #[cfg(test)]
    pub fn fail_delete_gen_entropy_for_scope() -> ScopedDeleteGenEntropyFailure {
        FORCE_DELETE_GEN_ENTROPY_FAILURE.with(|flag| flag.set(true));
        ScopedDeleteGenEntropyFailure
    }

    fn splitmix64(seed: u64) -> u64 {
        let mut z = seed.wrapping_add(0x9E37_79B9_7F4A_7C15);
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    fn lower_hex(bytes: &[u8]) -> String {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut out = String::with_capacity(bytes.len() * 2);
        for byte in bytes {
            out.push(HEX[usize::from(byte >> 4)] as char);
            out.push(HEX[usize::from(byte & 0x0f)] as char);
        }
        out
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

    pub fn delete_gen_128() -> Result<String, getrandom::Error> {
        #[cfg(test)]
        if FORCE_DELETE_GEN_ENTROPY_FAILURE.with(Cell::get) {
            return Err(getrandom::Error::UNSUPPORTED);
        }

        let mut bytes = [0_u8; 16];
        getrandom::getrandom(&mut bytes)?;
        Ok(lower_hex(&bytes))
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

    /// Claim an eviction candidate under this boot's lease context, using the
    /// server wall clock for the recency/suppress comparisons.
    ///
    /// # Errors
    ///
    /// Returns [`DbError`] if a statement fails.
    pub fn claim_eviction_candidate(
        &self,
        conn: &mut Connection,
        archive_item_id: i64,
        recency_floor_epoch: i64,
        allow_undurable: bool,
    ) -> Result<Option<String>, DbError> {
        claim_eviction_candidate(
            conn,
            &self.boot_id,
            self.mono_now_ms(),
            now_epoch_s(),
            archive_item_id,
            recency_floor_epoch,
            allow_undurable,
        )
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
    let generation = token::delete_gen_128().map_err(delete_gen_entropy_error)?;
    tx.execute(
        "UPDATE archive_items
            SET delete_state = 'DELETE_CLAIMED', delete_gen = ?2, updated_at = ?3
          WHERE id = ?1",
        params![archive_item_id, generation, now_epoch_s()],
    )?;
    tx.commit()?;
    Ok(Some(generation))
}

/// Atomically claim an archive item for eviction, re-checking the FULL
/// hard-delete allowlist predicate inside one transaction (defense-in-depth
/// for a permanent-loss op: a stale candidate list can never delete a row
/// that has since become ineligible). Aborts (returns `None`) on any
/// unexpired lease OR if the row is not: `LIVE`, `folder_class='RecentClips'`,
/// `pinned=0`, (`PROVEN_DURABLE` OR `allow_undurable`) where
/// `PROVEN_DURABLE := durable=1 AND finalized current COMPLETE upload set`,
/// all linked clips with `folder_class='RecentClips'` and known
/// (`started_at > 0`) recording time, and newest linked
/// `started_at < recency_floor_epoch`,
/// and unsuppressed (`suppress_until` `NULL` or `< now_epoch`). On success
/// transitions `LIVE -> DELETE_CLAIMED`, records a fresh random `delete_gen`,
/// and returns it.
///
/// # Errors
///
/// Returns [`DbError`] if a statement fails.
pub fn claim_eviction_candidate(
    conn: &mut Connection,
    boot_id: &str,
    mono_now_ms: i64,
    now_epoch: i64,
    archive_item_id: i64,
    recency_floor_epoch: i64,
    allow_undurable: bool,
) -> Result<Option<String>, DbError> {
    let tx = conn.transaction()?;
    if has_unexpired_lease(&tx, boot_id, mono_now_ms, archive_item_id)? {
        drop(tx);
        return Ok(None);
    }
    let eligible_sql = format!(
        // Fail-closed recency gate on clips.started_at (true recording instant,
        // epoch-seconds), mirroring list_eviction_candidates: every linked clip
        // must be RecentClips, non-Sentry, with a known (>0) start, and the
        // newest must precede recency_floor_epoch (also epoch-seconds).
        "SELECT EXISTS (
                 SELECT 1
                   FROM archive_items AS ai
                   JOIN archive_item_clips AS aic ON aic.archive_item_id = ai.id
                   JOIN clips AS c ON c.id = aic.clip_id
                  WHERE ai.id = ?1
                    AND ai.delete_state = 'LIVE'
                    AND ai.folder_class = 'RecentClips'
                    AND ai.pinned = 0
                    AND (?2 = 1 OR {PROVEN_DURABLE_PROOF_SQL})
                    AND (ai.suppress_until IS NULL OR ai.suppress_until < ?4)
                  GROUP BY ai.id
                 HAVING MIN(CASE WHEN c.folder_class = 'RecentClips' THEN 1 ELSE 0 END) = 1
                    AND MAX(c.is_sentry) = 0
                    AND MIN(CASE WHEN c.started_at > 0 THEN 1 ELSE 0 END) = 1
                    AND MAX(c.started_at) < ?3
             )"
    );
    let eligible: bool = tx
        .query_row(
            &eligible_sql,
            params![
                archive_item_id,
                i64::from(allow_undurable),
                recency_floor_epoch,
                now_epoch
            ],
            |r| r.get(0),
        )
        .optional()?
        .unwrap_or(false);
    if !eligible {
        drop(tx);
        return Ok(None);
    }
    let generation = token::delete_gen_128().map_err(delete_gen_entropy_error)?;
    tx.execute(
        "UPDATE archive_items
            SET delete_state = 'DELETE_CLAIMED', delete_gen = ?2, updated_at = ?3
          WHERE id = ?1",
        params![archive_item_id, generation, now_epoch],
    )?;
    tx.commit()?;
    Ok(Some(generation))
}

fn delete_gen_entropy_error(err: getrandom::Error) -> DbError {
    DbError::Sqlite(rusqlite::Error::InvalidParameterName(format!(
        "failed to obtain OS entropy for delete_gen: {err}"
    )))
}

/// Set an archive item's `delete_state` without precondition checks.
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

fn reject_transition(message: String) -> DbError {
    DbError::Sqlite(rusqlite::Error::InvalidParameterName(message))
}

fn transition_delete_state(
    conn: &Connection,
    archive_item_id: i64,
    from_state: &str,
    to_state: &str,
    bytes_freed: Option<i64>,
) -> Result<(), DbError> {
    let changed = conn.execute(
        "UPDATE archive_items
            SET delete_state = ?3,
                bytes_freed  = COALESCE(?4, bytes_freed),
                updated_at   = ?5
          WHERE id = ?1
            AND delete_state = ?2",
        params![
            archive_item_id,
            from_state,
            to_state,
            bytes_freed,
            now_epoch_s()
        ],
    )?;
    if changed == 1 {
        return Ok(());
    }
    if changed != 0 {
        return Err(reject_transition(format!(
            "archive item {archive_item_id} transition {from_state}->{to_state} touched {changed} rows"
        )));
    }
    let current_state: Option<String> = conn
        .query_row(
            "SELECT delete_state FROM archive_items WHERE id = ?1",
            params![archive_item_id],
            |row| row.get(0),
        )
        .optional()?;
    match current_state {
        Some(current_state) => Err(reject_transition(format!(
            "archive item {archive_item_id} transition {from_state}->{to_state} rejected from state {current_state}"
        ))),
        None => Err(reject_transition(format!(
            "archive item {archive_item_id} not found"
        ))),
    }
}

/// `DELETE_CLAIMED → DELETING` (D3 §4 step 4).
///
/// # Errors
/// Returns [`DbError`] on failure.
pub fn mark_deleting(conn: &Connection, archive_item_id: i64) -> Result<(), DbError> {
    transition_delete_state(conn, archive_item_id, "DELETE_CLAIMED", "DELETING", None)
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
    if bytes_freed < 0 {
        return Err(reject_transition("bytes_freed must be >= 0".to_owned()));
    }
    transition_delete_state(
        conn,
        archive_item_id,
        "DELETING",
        "DELETED",
        Some(bytes_freed),
    )
}

/// Release a delete claim back to `LIVE` (D3 §4.1 recovery).
///
/// # Errors
/// Returns [`DbError`] on failure.
pub fn release_delete_claim(conn: &Connection, archive_item_id: i64) -> Result<(), DbError> {
    transition_delete_state(conn, archive_item_id, "DELETE_CLAIMED", "LIVE", None)
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

/// Upsert one settings preference in `prefs`.
///
/// # Errors
///
/// Returns [`DbError`] if validation fails or the statement fails.
pub fn set_pref(conn: &Connection, key: &str, value: &str) -> Result<(), DbError> {
    if key.is_empty() || key.len() > 64 {
        return Err(DbError::Sqlite(rusqlite::Error::InvalidParameterName(
            "prefs.key must be 1..=64 bytes".to_owned(),
        )));
    }
    if !key
        .bytes()
        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
    {
        return Err(DbError::Sqlite(rusqlite::Error::InvalidParameterName(
            "prefs.key must match [a-z0-9_]+".to_owned(),
        )));
    }
    // Empty values are permitted: an empty `display_timezone` is the "Auto /
    // use device-local time" sentinel that webd validates and forwards (see
    // webd `validate_setting` + the `put_settings_display_timezone_empty`
    // test). Rejecting it here broke the settings form's "Auto" option.
    if value.len() > 8192 {
        return Err(DbError::Sqlite(rusqlite::Error::InvalidParameterName(
            "prefs.value must be at most 8192 bytes".to_owned(),
        )));
    }

    conn.execute(
        "INSERT INTO prefs (key, value) VALUES (?1, ?2)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        params![key, value],
    )?;
    Ok(())
}

/// Get one settings preference value from `prefs` by `key`.
///
/// # Errors
///
/// Returns [`DbError`] if the query fails.
pub fn get_pref(conn: &Connection, key: &str) -> Result<Option<String>, DbError> {
    let value = conn
        .query_row(
            "SELECT value FROM prefs WHERE key = ?1",
            params![key],
            |r| r.get(0),
        )
        .optional()?;
    Ok(value)
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
        ClipLeaseGrant, LeaseGrant, LeaseKind, ReleaseResult, RenewResult,
        claim_eviction_candidate, claim_for_delete, get_pref, has_unexpired_lease, lease_acquire,
        lease_acquire_for_clip, lease_release, lease_renew, mark_deleted, mark_deleting,
        reap_stale_leases, release_delete_claim, set_pref, wal_checkpoint_truncate,
    };
    use crate::db::open_in_memory;

    const BOOT: &str = "boot-current";
    const TTL: u32 = 60;

    #[derive(Debug, Clone)]
    struct ProvingUploadSetFixture {
        upload_set_id: String,
        digest: String,
        destination_id: String,
        remote_key: String,
        child_key: String,
    }

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

    fn insert_eviction_item(
        conn: &Connection,
        folder_class: &str,
        archived_at: i64,
        durable: i64,
        pinned: i64,
        suppress_until: Option<i64>,
        delete_state: &str,
    ) -> i64 {
        let path = format!(
            "archive/{folder_class}/{archived_at}-{durable}-{pinned}-{}",
            suppress_until.unwrap_or(-1)
        );
        conn.execute(
            "INSERT INTO archive_items
               (folder_class, path, size_bytes, file_count, archived_at, delete_state,
                 durable, pinned, suppress_until, created_at, updated_at)
             VALUES (?1, ?2, 4096, 1, ?3, ?4, ?5, ?6, ?7, 0, 0)",
            params![
                folder_class,
                path.clone(),
                archived_at,
                delete_state,
                durable,
                pinned,
                suppress_until
            ],
        )
        .unwrap();
        let archive_item_id = conn.last_insert_rowid();
        if durable == 1 {
            seed_proving_upload_set(conn, archive_item_id);
        }
        insert_clip_for_eviction(conn, archive_item_id, &path, archived_at, folder_class);
        archive_item_id
    }

    fn insert_eviction_item_unlinked(
        conn: &Connection,
        folder_class: &str,
        archived_at: i64,
        durable: i64,
        pinned: i64,
        suppress_until: Option<i64>,
        delete_state: &str,
    ) -> i64 {
        conn.execute(
            "INSERT INTO archive_items
                (folder_class, path, size_bytes, file_count, archived_at, delete_state,
                 durable, pinned, suppress_until, created_at, updated_at)
             VALUES (?1, ?2, 4096, 1, ?3, ?4, ?5, ?6, ?7, 0, 0)",
            params![
                folder_class,
                format!(
                    "archive/{folder_class}/{archived_at}-{durable}-{pinned}-unlinked-{}",
                    suppress_until.unwrap_or(-1)
                ),
                archived_at,
                delete_state,
                durable,
                pinned,
                suppress_until
            ],
        )
        .unwrap();
        let archive_item_id = conn.last_insert_rowid();
        if durable == 1 {
            seed_proving_upload_set(conn, archive_item_id);
        }
        archive_item_id
    }

    fn insert_clip_for_eviction(
        conn: &Connection,
        archive_item_id: i64,
        key_suffix: &str,
        started_at: i64,
        folder_class: &str,
    ) {
        conn.execute(
            "INSERT INTO clips (canonical_key, started_at, partition, folder_class, created_at, updated_at)
             VALUES (?1, ?2, 'p', ?3, 0, 0)",
            params![format!("clip:{key_suffix}"), started_at, folder_class],
        )
        .unwrap();
        let clip_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO archive_item_clips (archive_item_id, clip_id) VALUES (?1, ?2)",
            params![archive_item_id, clip_id],
        )
        .unwrap();
    }

    fn proving_upload_set_fixture(archive_item_id: i64) -> ProvingUploadSetFixture {
        ProvingUploadSetFixture {
            upload_set_id: format!("{archive_item_id:032x}"),
            digest: format!("{:032x}", archive_item_id + 0x1000),
            destination_id: format!("dest-{archive_item_id}"),
            remote_key: format!("remote-{archive_item_id}"),
            child_key: format!("child-{archive_item_id}"),
        }
    }

    fn seed_proving_upload_set(conn: &Connection, archive_item_id: i64) -> ProvingUploadSetFixture {
        let fixture = proving_upload_set_fixture(archive_item_id);
        let request_digest = format!("{archive_item_id:064x}");
        let content_sha256 = "a".repeat(64);
        conn.execute(
            "UPDATE archive_items SET manifest_digest = ?2 WHERE id = ?1",
            params![archive_item_id, fixture.digest.as_str()],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cloud_parent_upload_sets
                (upload_set_id, archive_item_id, destination_id, source_manifest_digest, request_digest,
                 expected_child_count, created_at, finalized_at, superseded_at)
             VALUES (?1, ?2, ?3, ?4, ?5, 1, 0, 1, NULL)",
            params![
                fixture.upload_set_id.as_str(),
                archive_item_id,
                fixture.destination_id.as_str(),
                fixture.digest.as_str(),
                request_digest
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cloud_parent_upload_set_children
                (upload_set_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 manifest_mtime_ms, content_sha256, expected_hash, verify_alg)
             VALUES (?1, ?2, ?3, ?4, 'bulk', 0, 4096, 0, ?5, 'expected-hash', 'sha256')",
            params![
                fixture.upload_set_id.as_str(),
                fixture.child_key.as_str(),
                fixture.destination_id.as_str(),
                fixture.remote_key.as_str(),
                content_sha256
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO cloud_upload_queue
                (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 bytes_uploaded, expected_hash, verify_alg, content_sha256, state, attempts, upload_set_id)
             VALUES (?1, ?2, ?3, ?4, 'bulk', 0, 4096, 4096, 'expected-hash', 'sha256', ?5, 'done', 0, ?6)",
            params![
                archive_item_id,
                fixture.child_key.as_str(),
                fixture.destination_id.as_str(),
                fixture.remote_key.as_str(),
                content_sha256,
                fixture.upload_set_id.as_str()
            ],
        )
        .unwrap();
        fixture
    }

    fn insert_extra_queue_row_for_set(
        conn: &Connection,
        archive_item_id: i64,
        fixture: &ProvingUploadSetFixture,
    ) {
        let extra_child_key = format!("{}-extra", fixture.child_key);
        let extra_destination_id = format!("{}-extra", fixture.destination_id);
        let extra_remote_key = format!("{}-extra", fixture.remote_key);
        let extra_content_sha256 = "b".repeat(64);
        conn.execute(
            "INSERT INTO cloud_upload_queue
                (archive_item_id, child_key, destination_id, remote_key, category, seq, total_bytes,
                 bytes_uploaded, expected_hash, verify_alg, content_sha256, state, attempts, upload_set_id)
             VALUES (?1, ?2, ?3, ?4, 'bulk', 99, 1, 0, 'extra-expected-hash', 'sha256', ?5, 'done', 0, ?6)",
            params![
                archive_item_id,
                extra_child_key,
                extra_destination_id,
                extra_remote_key,
                extra_content_sha256,
                fixture.upload_set_id.as_str()
            ],
        )
        .unwrap();
    }

    fn break_arm_a(conn: &Connection, archive_item_id: i64, _: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE archive_items SET durable = 0 WHERE id = ?1",
            params![archive_item_id],
        )
        .unwrap();
    }

    fn break_arm_f(conn: &Connection, archive_item_id: i64, _: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE archive_items SET manifest_digest = NULL WHERE id = ?1",
            params![archive_item_id],
        )
        .unwrap();
    }

    fn break_arm_g(conn: &Connection, archive_item_id: i64, _: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE archive_items
                SET manifest_digest = 'ffffffffffffffffffffffffffffffff'
              WHERE id = ?1",
            params![archive_item_id],
        )
        .unwrap();
    }

    fn break_arm_i(conn: &Connection, archive_item_id: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_parent_upload_sets
                SET expected_child_count = 2
              WHERE upload_set_id = ?1",
            params![fixture.upload_set_id.as_str()],
        )
        .unwrap();
        insert_extra_queue_row_for_set(conn, archive_item_id, fixture);
    }

    fn break_arm_j(conn: &Connection, archive_item_id: i64, fixture: &ProvingUploadSetFixture) {
        insert_extra_queue_row_for_set(conn, archive_item_id, fixture);
    }

    fn break_arm_k(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        let mismatched_remote_key = format!("{}-mismatch", fixture.remote_key);
        conn.execute(
            "UPDATE cloud_upload_queue
                SET remote_key = ?3
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![
                fixture.upload_set_id.as_str(),
                fixture.child_key.as_str(),
                mismatched_remote_key
            ],
        )
        .unwrap();
    }

    fn break_arm_n(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET verify_alg = 'md5'
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .unwrap();
    }

    fn break_arm_o_null(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET expected_hash = NULL
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .unwrap();
    }

    fn break_arm_o_differs(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET expected_hash = 'different-hash'
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .unwrap();
    }

    fn break_arm_p(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET child_key = 'other-child'
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .unwrap();
    }

    fn break_arm_q(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET category = 'trip'
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .unwrap();
    }

    fn break_arm_r(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET seq = 1
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .unwrap();
    }

    fn break_arm_s(conn: &Connection, _: i64, fixture: &ProvingUploadSetFixture) {
        conn.execute(
            "UPDATE cloud_upload_queue
                SET total_bytes = 8192
              WHERE upload_set_id = ?1
                AND child_key = ?2",
            params![fixture.upload_set_id.as_str(), fixture.child_key.as_str()],
        )
        .unwrap();
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
    fn claim_for_delete_entropy_failure_keeps_row_live_and_unset() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "/a");

        let _entropy_guard = super::token::fail_delete_gen_entropy_for_scope();
        let err = claim_for_delete(&mut conn, BOOT, 1_000, item)
            .unwrap_err()
            .to_string();
        assert!(err.contains("failed to obtain OS entropy for delete_gen"));

        let (state, generation): (String, Option<String>) = conn
            .query_row(
                "SELECT delete_state, delete_gen FROM archive_items WHERE id = ?1",
                params![item],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(state, "LIVE");
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_claims_eligible_old_durable_row() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert!(generation.is_some());
        let state: String = conn
            .query_row(
                "SELECT delete_state FROM archive_items WHERE id = ?1",
                params![item],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(state, "DELETE_CLAIMED");
    }

    #[test]
    fn claim_eviction_candidate_entropy_failure_keeps_row_live_and_unset() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");

        let _entropy_guard = super::token::fail_delete_gen_entropy_for_scope();
        let err = claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false)
            .unwrap_err()
            .to_string();
        assert!(err.contains("failed to obtain OS entropy for delete_gen"));

        let (state, generation): (String, Option<String>) = conn
            .query_row(
                "SELECT delete_state, delete_gen FROM archive_items WHERE id = ?1",
                params![item],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(state, "LIVE");
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_returns_none_with_unexpired_lease() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        lease_acquire(&conn, BOOT, 0, item, LeaseKind::Playback, "webd", TTL).unwrap();

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_returns_none_when_too_recent() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 500, 1, 0, None, "LIVE");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_honors_allow_undurable_flag() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 0, 0, None, "LIVE");

        let denied =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(denied, None);

        let claimed =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, true).unwrap();
        assert!(claimed.is_some());
    }

    #[test]
    fn claim_eviction_candidate_returns_none_when_pinned() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 1, None, "LIVE");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_returns_none_for_sentry_row() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "SentryClips", 100, 1, 0, None, "LIVE");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_returns_none_when_suppressed() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, Some(2_000), "LIVE");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_returns_none_when_not_live() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "DELETE_CLAIMED");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_excludes_item_with_no_linked_clip() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item_unlinked(&conn, "RecentClips", 100, 1, 0, None, "LIVE");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_excludes_item_with_zero_started_at() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item_unlinked(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        insert_clip_for_eviction(&conn, item, "zero-started-at", 0, "RecentClips");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_excludes_when_linked_clip_not_recentclips() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item_unlinked(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        insert_clip_for_eviction(&conn, item, "mismatched-folder-class", 100, "SentryClips");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_excludes_stale_archived_at_but_fresh_started_at() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item_unlinked(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        insert_clip_for_eviction(
            &conn,
            item,
            "stale-archived-fresh-started",
            9_500,
            "RecentClips",
        );

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_deletes_when_started_at_old_even_if_archived_at_fresh() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item_unlinked(&conn, "RecentClips", 9_500, 1, 0, None, "LIVE");
        insert_clip_for_eviction(
            &conn,
            item,
            "fresh-archived-old-started",
            100,
            "RecentClips",
        );

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert!(generation.is_some());
    }

    #[test]
    fn claim_eviction_candidate_rejects_stale_durable_without_proving_set() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        conn.execute(
            "DELETE FROM cloud_upload_queue WHERE archive_item_id = ?1",
            params![item],
        )
        .unwrap();
        conn.execute(
            "DELETE FROM cloud_parent_upload_set_children WHERE upload_set_id = ?1",
            params![format!("{item:032x}")],
        )
        .unwrap();
        conn.execute(
            "DELETE FROM cloud_parent_upload_sets WHERE archive_item_id = ?1",
            params![item],
        )
        .unwrap();

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_rejects_superseded_proving_set() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        conn.execute(
            "UPDATE cloud_parent_upload_sets SET superseded_at = 2 WHERE archive_item_id = ?1",
            params![item],
        )
        .unwrap();

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_rejects_unfinalized_proving_set() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        conn.execute(
            "UPDATE cloud_parent_upload_sets SET finalized_at = NULL WHERE archive_item_id = ?1",
            params![item],
        )
        .unwrap();

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_rejects_incomplete_proving_set_when_queue_state_not_done() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        conn.execute(
            "UPDATE cloud_upload_queue SET state = 'queued' WHERE archive_item_id = ?1",
            params![item],
        )
        .unwrap();

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_rejects_incomplete_proving_set_when_content_sha_differs() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        conn.execute(
            "UPDATE cloud_upload_queue
                SET content_sha256 = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
              WHERE archive_item_id = ?1",
            params![item],
        )
        .unwrap();

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_allows_fully_proven_item_without_allow_undurable() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert!(generation.is_some());
    }

    #[test]
    fn claim_eviction_candidate_allow_undurable_bypasses_proven_durable_gate() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        conn.execute(
            "DELETE FROM cloud_upload_queue WHERE archive_item_id = ?1",
            params![item],
        )
        .unwrap();
        conn.execute(
            "DELETE FROM cloud_parent_upload_set_children WHERE upload_set_id = ?1",
            params![format!("{item:032x}")],
        )
        .unwrap();
        conn.execute(
            "DELETE FROM cloud_parent_upload_sets WHERE archive_item_id = ?1",
            params![item],
        )
        .unwrap();

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, true).unwrap();
        assert!(generation.is_some());
    }

    #[test]
    #[allow(clippy::too_many_lines)]
    fn claim_eviction_candidate_rejects_each_proven_durable_arm_break() {
        type ArmMutator = fn(&Connection, i64, &ProvingUploadSetFixture);
        let cases: [(&str, &str, ArmMutator); 13] = [
            ("arm_a_durable_bit", "ai.durable = 1", break_arm_a),
            (
                "arm_f_manifest_digest_non_null",
                "ai.manifest_digest IS NOT NULL",
                break_arm_f,
            ),
            (
                "arm_g_manifest_digest_matches_parent",
                "ai.manifest_digest = pus.source_manifest_digest",
                break_arm_g,
            ),
            (
                "arm_i_expected_equals_child_count",
                "SELECT COUNT(*) FROM cloud_parent_upload_set_children pusc",
                break_arm_i,
            ),
            (
                "arm_j_expected_equals_queue_count",
                "SELECT COUNT(*) FROM cloud_upload_queue puq",
                break_arm_j,
            ),
            (
                "arm_k_joined_queue_row_must_exist",
                "pq.upload_set_id IS NULL",
                break_arm_k,
            ),
            (
                "arm_n_verify_alg_matches",
                "pq.verify_alg <> pm.verify_alg",
                break_arm_n,
            ),
            (
                "arm_o_expected_hash_null",
                "COALESCE(pq.expected_hash, '') <> pm.expected_hash",
                break_arm_o_null,
            ),
            (
                "arm_o_expected_hash_differs",
                "COALESCE(pq.expected_hash, '') <> pm.expected_hash",
                break_arm_o_differs,
            ),
            (
                "arm_p_child_key_matches",
                "pq.child_key <> pm.child_key",
                break_arm_p,
            ),
            (
                "arm_q_category_matches",
                "pq.category <> pm.category",
                break_arm_q,
            ),
            ("arm_r_seq_matches", "pq.seq <> pm.seq", break_arm_r),
            (
                "arm_s_total_bytes_matches",
                "pq.total_bytes <> pm.total_bytes",
                break_arm_s,
            ),
        ];
        // Arm h (expected_child_count > 0) is schema-enforced by
        // cloud_parent_upload_sets.expected_child_count CHECK(expected_child_count > 0).
        let mut unexpectedly_claimed = Vec::new();
        for (case_name, required_clause, mutate) in cases {
            assert!(
                crate::db::PROVEN_DURABLE_PROOF_SQL.contains(required_clause),
                "case {case_name}: required proof clause missing from PROVEN_DURABLE_PROOF_SQL"
            );
            let mut conn = open_in_memory().unwrap();
            let sanity_item = insert_eviction_item(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
            let sanity =
                claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, sanity_item, 500, false)
                    .unwrap();
            assert!(
                sanity.is_some(),
                "case {case_name}: baseline proven seed must be claimable before mutation"
            );
            let candidate_item =
                insert_eviction_item(&conn, "RecentClips", 101, 1, 0, None, "LIVE");
            let fixture = proving_upload_set_fixture(candidate_item);
            mutate(&conn, candidate_item, &fixture);
            let claim =
                claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, candidate_item, 500, false)
                    .unwrap();
            if claim.is_some() {
                unexpectedly_claimed.push(case_name);
            }
        }
        assert!(
            unexpectedly_claimed.is_empty(),
            "claim_eviction_candidate unexpectedly accepted broken-proof cases: {unexpectedly_claimed:?}"
        );
    }

    fn insert_sentry_flagged_recent_clip_for_eviction(
        conn: &Connection,
        archive_item_id: i64,
        key_suffix: &str,
        started_at: i64,
    ) {
        conn.execute(
            "INSERT INTO clips
                (canonical_key, started_at, partition, folder_class, is_sentry, created_at, updated_at)
             VALUES (?1, ?2, 'p', 'RecentClips', 1, 0, 0)",
            params![format!("clip:{key_suffix}"), started_at],
        )
        .unwrap();
        let clip_id = conn.last_insert_rowid();
        conn.execute(
            "INSERT INTO archive_item_clips (archive_item_id, clip_id) VALUES (?1, ?2)",
            params![archive_item_id, clip_id],
        )
        .unwrap();
    }

    #[test]
    fn claim_eviction_candidate_excludes_multiclip_item_when_any_clip_is_fresh() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item_unlinked(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        // Old + fresh segment on one item: MAX(started_at) must keep it ineligible.
        insert_clip_for_eviction(&conn, item, "multiclip-old", 100, "RecentClips");
        insert_clip_for_eviction(&conn, item, "multiclip-fresh", 9_500, "RecentClips");

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn claim_eviction_candidate_excludes_item_with_sentry_flagged_recent_clip() {
        let mut conn = open_in_memory().unwrap();
        let item = insert_eviction_item_unlinked(&conn, "RecentClips", 100, 1, 0, None, "LIVE");
        insert_sentry_flagged_recent_clip_for_eviction(&conn, item, "sentry-flagged", 100);

        let generation =
            claim_eviction_candidate(&mut conn, BOOT, 1_000, 1_000, item, 500, false).unwrap();
        assert_eq!(generation, None);
    }

    #[test]
    fn delete_state_finishers() {
        let conn = open_in_memory().unwrap();
        let item = insert_archive_item(&conn, "/a");
        conn.execute(
            "UPDATE archive_items
                SET delete_state = 'DELETE_CLAIMED', delete_gen = 'g1'
              WHERE id = ?1",
            params![item],
        )
        .unwrap();
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
    }

    #[test]
    fn guarded_delete_transitions_reject_wrong_states_and_missing_rows() {
        let conn = open_in_memory().unwrap();
        let live = insert_archive_item(&conn, "/live");
        let claimed = insert_archive_item(&conn, "/claimed");
        conn.execute(
            "UPDATE archive_items
                SET delete_state = 'DELETE_CLAIMED', delete_gen = 'g2'
              WHERE id = ?1",
            params![claimed],
        )
        .unwrap();
        let deleted = insert_archive_item(&conn, "/deleted");
        conn.execute(
            "UPDATE archive_items
                SET delete_state = 'DELETED'
              WHERE id = ?1",
            params![deleted],
        )
        .unwrap();

        let err = mark_deleting(&conn, live).unwrap_err().to_string();
        assert!(err.contains("DELETE_CLAIMED->DELETING rejected from state LIVE"));
        let err = mark_deleting(&conn, 9_999).unwrap_err().to_string();
        assert!(err.contains("not found"));

        mark_deleting(&conn, claimed).unwrap();
        let err = mark_deleted(&conn, deleted, 10).unwrap_err().to_string();
        assert!(err.contains("DELETING->DELETED rejected from state DELETED"));
        let err = mark_deleted(&conn, 9_999, 10).unwrap_err().to_string();
        assert!(err.contains("not found"));
        let err = mark_deleted(&conn, claimed, -1).unwrap_err().to_string();
        assert!(err.contains("bytes_freed must be >= 0"));

        let err = release_delete_claim(&conn, live).unwrap_err().to_string();
        assert!(err.contains("DELETE_CLAIMED->LIVE rejected from state LIVE"));
        let err = release_delete_claim(&conn, 9_999).unwrap_err().to_string();
        assert!(err.contains("not found"));
    }

    #[test]
    fn set_pref_upserts_existing_key() {
        let conn = open_in_memory().unwrap();
        set_pref(&conn, "speed_unit", "mph").unwrap();
        set_pref(&conn, "speed_unit", "kph").unwrap();
        let value: String = conn
            .query_row(
                "SELECT value FROM prefs WHERE key = 'speed_unit'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(value, "kph");
    }

    #[test]
    fn set_pref_rejects_invalid_key_and_writes_nothing() {
        let conn = open_in_memory().unwrap();
        assert!(set_pref(&conn, "Bad Key", "mph").is_err());
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM prefs WHERE key = 'Bad Key'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn set_pref_rejects_overlong_value_and_writes_nothing() {
        let conn = open_in_memory().unwrap();
        let value = "x".repeat(8193);
        assert!(set_pref(&conn, "speed_unit", &value).is_err());
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM prefs WHERE key = 'speed_unit'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn set_pref_allows_empty_value_and_round_trips() {
        // An empty `display_timezone` is the "Auto" sentinel webd forwards; the
        // store must accept it and round-trip it (it was previously rejected,
        // which broke the settings form's "Auto (device local)" option).
        let conn = open_in_memory().unwrap();
        set_pref(&conn, "display_timezone", "").unwrap();
        assert_eq!(
            get_pref(&conn, "display_timezone").unwrap().as_deref(),
            Some("")
        );
    }

    #[test]
    fn get_pref_returns_value_when_present() {
        let conn = open_in_memory().unwrap();
        set_pref(&conn, "speed_unit", "kph").unwrap();
        let value = get_pref(&conn, "speed_unit").unwrap();
        assert_eq!(value.as_deref(), Some("kph"));
    }

    #[test]
    fn get_pref_returns_none_when_absent() {
        let conn = open_in_memory().unwrap();
        let value = get_pref(&conn, "speed_unit").unwrap();
        assert!(value.is_none());
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
