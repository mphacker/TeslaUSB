# SPEC — `uploadd` (durable cloud upload queue)

> Parent: [`SPEC.md`](./SPEC.md) · Criticality: disposable · Language: Rust
> Reference behavior: `web/teslausb_web/services/cloud_archive/*`.

## 1. Objective

Upload archived media to a configured cloud remote **durably, resumably, and
throttled**, prioritized by user policy, sourced **only** from the Pi-side
**archive directory** — never from the live car LUN — and **never** triggering a
reboot or gadget restart.

## 2. Responsibilities

1. **Durable queue** in SQLite: enqueue archive items, track state
   (queued/in-progress/done/failed), retain enough for resumable, idempotent
   re-runs (reference: `queue_ops.py`, `pipeline.py`, `reconcile.py`).
2. **Transfer** via **rclone** *or* a small Rust uploader (**open: choose at
   build** — rclone for provider breadth vs. a Rust uploader for footprint):
   resumable, checksum-verified, integrity-checked (reference: `uploader.py`,
   `integrity.py`). On
   success, mark the item **`UPLOADED_VERIFIED` (durable)** in the index — this is
   what lets `retentiond` treat the local copy as safe to evict. **`uploadd` never
   deletes Pi-side archive files** (single-deleter = `retentiond`,
   [`storage.md` §5](./storage.md)); while transferring it holds an **upload
   lease (TTL, renewed by heartbeat while active)** on the item so eviction can't
   race the read.
3. **Throttling:** respect the WiFi TX rate cap coordinated with `wifid` (limited
   WiFi time; never saturate the link or trip the SDIO deadlock threshold).
4. **Prioritization:** upload order driven by **user policy** (e.g. events/Sentry
   first, then trips, then bulk) — preserve today's prioritized behavior.
5. **Provider/OAuth + remote config** management (reference: `provider.js`,
   `cloud_oauth_service.py`, `cloud_rclone_service.py`, `settings.py`); expose
   status/queue to `webd` for the cloud-archive UI.
6. **Cleanup/retention of cloud copies** per policy (reference:
   `cloud_cleanup.py`) so the remote doesn't grow unbounded.

## 3. Non-responsibilities

- Never reads from the live car LUN; only the archive directory (populated by
  `retentiond`).
- Never reboots the Pi or restarts the gadget; failures are retried in-queue.
- Does not decide *what* to archive (that is `retentiond`); it uploads what's in
  the archive per the queue/policy. **Never deletes Pi-side archive files** — it
  only marks durability and holds upload leases; `retentiond` is the sole deleter.

## 4. Acceptance criteria

- [ ] Queue survives restart/power loss; in-flight items resume without
      duplication or corruption (checksum-verified).
- [ ] Uploads stay under the coordinated WiFi TX cap; never trip the SDIO
      deadlock; never starve the car's I/O.
- [ ] Priority order matches configured policy.
- [ ] Sources only from the archive directory; live LUN is never read.
- [ ] Runs within `MemoryMax`; is first in the OOM kill order (most disposable).

## 5. Testing

- Queue state-machine tests (enqueue/resume/idempotency/failure-retry).
- Integrity tests (corrupt/partial transfer → detected and retried).
- Throttle coordination test with a mocked `wifid` token bucket.

## 6. Boundaries

**ALWAYS** source from the archive directory; resume idempotently; honor the WiFi
throttle and priority policy.
**ASK FIRST** before changing prioritization semantics or adding a new provider
type.
**NEVER** read the live LUN; never reboot/restart the gadget; never exceed the
WiFi TX cap; never block car I/O.
