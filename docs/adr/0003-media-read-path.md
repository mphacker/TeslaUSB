# ADR 0003 — Media read path: RO loop-mount for `media.img`, raw `pread` for live TeslaCam

- **Status:** Accepted
- **Date:** 2026-06-12
- **Deciders:** operator; Opus (planning) reconciled with GPT-5.5 + mai-code-1-flash adversarial reviews
- **Scope:** `gadgetd`, `scannerd`, `webd`, `docs/specs/{SPEC,scannerd,usb-io-and-archiving-architecture,webd}.md`, `docs/specs/contracts/scannerd-readfile.md`

## Context

The web UI must serve **file bytes** off the USB images so the user can play
media audio (music/boombox/light-show), preview the **Active Lock Chime**
(`LockChime.wav`), see wrap/plate thumbnails, and play recorded TeslaCam clips
on the trip map. None of this is implemented today: `webd` streams clips only
from the Pi-side ext4 archive (which is not yet being filled), and media bytes
are unserved.

A prior design (`docs/specs/contracts/scannerd-readfile.md`) answered this with
a single, general, heavyweight **content-read seam**: a two-RPC handle model
(`Resolve`→`ReadHandle`, then `ReadRange`), a per-slot `AtomicU64` **generation
counter** bumped on every scan pass and on handoff edges, and **per-chunk
identity revalidation** (re-reading the parent directory entry + exFAT
`SetChecksum` + `NameHash` before every byte window). The operator judged this
*complex and brittle* and directed a return to **simple, proven, battle-tested**
mechanisms, with a mandatory independent review from GPT-5.5 and mai.

Two volumes with very different concurrency:

- **`media.img` (lun.1):** the Pi is the **sole writer**, and only inside a
  `lun.1` eject-handoff window. Outside a handoff the image is **static**.
- **`teslacam.img` (lun.0):** the car **writes it continuously**; the Pi must
  never mount it (cache incoherence + write-path risk; the #1 invariant).

## Decision

Use the simplest proven tool for each volume; do **not** force one mechanism to
cover both.

1. **`media.img` reads → read-only kernel loop-mount.** `gadgetd` owns a single
   **persistent RO loop-mount** of `media.img` (the kernel exFAT driver — the
   same battle-tested path already used for the *write* handoff, minus the
   `rw`). `webd` reads media bytes through that mount with `std::fs` (range
   streaming as today's `media.rs` does for archive clips). No custom exFAT
   byte-server, no `ReadFile` RPC, no SD shadow copy, for any media file.
   - A **media handoff** quiesces this: acquire-drain in-flight reads via a
     short **read-lease**, unmount RO, do the RW mutate, re-mount RO. Because
     `media.img` is static outside that window, the RO mount is cache-coherent.

2. **`teslacam.img` reads → raw `pread`, never mounted.** Keep `scannerd`'s
   existing, tested raw reader. Map playback is **archive-first**: serve the
   durable ext4 archive copy whenever it exists; the not-yet-archived recent
   window falls back to a **bounded, best-effort** raw read — **catalog-`stable`
   clips only**, clamped to `valid_data_length`, with a **cheap identity handle**
   (first-cluster + size + name-hash captured once and echoed across the HTTP
   range loop) and **`410 Gone` on identity change** (never wrong bytes). This
   live fallback may be built *after* the archive loop runs, since archive-first
   covers the common case.

3. **Drop the heavyweight seam.** No generation counters, no per-chunk
   `SetChecksum` re-walk, no handoff-edge generation fencing. Handoff safety for
   media comes from the read-lease/quiesce (#1); live-clip safety comes from
   stable-only + clamp + identity-fence + `410` (#2).

4. **Enforce `lun.1` USB read-only (`ro=1`).** "The car only reads media" must be
   *enforced* in configfs, not assumed, so the Pi-sole-writer premise that makes
   the RO mount coherent cannot be violated by the car writing exFAT metadata.

## Why (reconciliation of the two independent reviews)

- **Both GPT-5.5 and mai independently recommended** splitting the read path: raw
  `pread` for the continuously-written `lun.0`, and a **RO loop-mount for the
  static `media.img`** as the simpler, more battle-tested media path. This also
  matches the operator's "use OS loops" instinct.
- **Both flagged that a *fully naive* stateless `ReadFile` is unsafe for range
  requests** on the live volume: separate range chunks could stitch bytes from
  different file generations after rotation/replacement, returning *wrong bytes
  with HTTP 200*. `404-on-miss` only catches a vanished path, not a replaced
  one. Hence the cheap identity handle + `410` for the lun.0 fallback (decision
  #2), rather than either extreme.
- **Both judged the current contract over-engineered** for the real risk and
  endorsed cutting the generation/per-chunk-checksum machinery.
- **GPT-5.5 additionally required** (a) draining in-flight reads before a media
  RW mount, and (b) enforcing `lun.1 ro=1` — folded in as decisions #1 and #4.

## Consequences

- **Simpler, less code.** `scannerd-readfile.md` collapses from a generic
  multi-volume seam to a small lun.0-only best-effort fallback (or is deferred
  entirely behind archive-first). Media playback is ordinary `std::fs` over a
  kernel mount.
- **New `gadgetd` responsibility:** own the persistent RO `media.img` mount and
  the read-lease/quiesce around a handoff. The handoff state machine gains a
  "drain readers → unmount RO → mutate RW → remount RO" path.
- **`webd`** gains a media-bytes streaming handler over the RO mount path
  (mirror `media.rs`), replacing the planned per-category `ReadFile` wiring.
- **Coupling:** media playback now depends on `gadgetd`'s RO mount being healthy.
  If the mount is down (e.g. mid-handoff), media reads return `503` and retry —
  acceptable, since handoffs are brief and operator-gated.
- **Reversible:** if a future need arises for fully-consistent live reads, the
  raw reader is still present; nothing here precludes adding a short-lived raw
  snapshot path later.

## Alternatives considered

- **One raw-reader `ReadFile` for everything** (the prior plan). Rejected:
  needless custom exFAT byte-serving for the static, common media case; the
  heavyweight consistency machinery it required is the complexity the operator
  asked to remove.
- **RO loop-mount of `teslacam.img` too.** Rejected: cache-incoherent under the
  car's continuous writes; risks the #1 invariant.
- **dm-thin / block snapshot under the live LUN.** Rejected previously
  (`plan.md`): CoW/pool-full → origin EIO → latch.
