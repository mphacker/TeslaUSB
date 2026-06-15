# TeslaUSB B-1 — Build Status (vs. `Requirements.md`)

> **What this is.** The single master checklist of *everything* needed to make
> the B-1 (Rust) solution match [`Requirements.md`](./Requirements.md) — v1's
> features and look-and-feel, re-implemented in Rust, more efficiently, with zero
> clip loss. Every item is a checkbox. **A box is checked ONLY when the behavior
> has been tested end-to-end and proven** (Playwright for UI, hardware-test wrapper
> for device behavior) — not when code merely compiles or an endpoint returns 200.
>
> **Authoritative inputs:** [`Requirements.md`](./Requirements.md) (the baseline),
> [`plan.md`](./plan.md) (honest status + tiers), [`specs/`](./specs/) and
> [`adr/`](./adr/) (locked architecture, incl. [`ADR-0003`](./adr/0003-media-read-path.md)
> media read path).

## ⏯️ Resume here — CHIME LIBRARY AUTO-REFRESH AFTER UPLOAD LIVE (2026-06-15)

**Operator requirement (locked, now satisfied):** after uploading a chime, the
**Chime Library** table must auto-refresh with clear confirmation — no manual
page reload. Previously the user had to reload to see the new chime.

✅ **Shipped + proven live on `cybertruckusb.local` (see `files/hw-results.md`
"Chime Library auto-refresh after upload" + screenshots
`files/hw-chime-autorefresh-{desktop-1280,mobile-375}.png`):**
- **Frontend-only design (D+A):** on upload success the Media screen records a
  `pendingUpload = {filename, bytes, token}` from the **client-known** file
  identity (`selectedFile.name`/`.size`) — the upload's `202 {state:"queued",
  job_id}` carries no filename/size. ChimeScheduler shows an optimistic
  "Syncing…" pending row and **bounded-polls** the snapshot (2 s interval, 45 s
  cap) until a catalog row matches **filename + EXACT byte size**, then clears the
  row and shows an "added to your chime library" notice. On timeout → "Waiting for
  media scan…" + a **Refresh now** button.
- **Filename parity:** the pending-row key mirrors webd `sanitise_filename`
  (basename + trim) so a space-padded upload still converges on hardware.
- **Convergence key = filename + exact byte size** (verbatim copy ⇒ uploaded
  `File.size` == catalog `size_bytes`), which also defeats the same-name-reupload
  false-positive (a same-name/different-size stale row is suppressed). Documented
  trade-off: same-name + byte-identical-length different content confirms early
  (cosmetic only; correct bytes still land).
- **Abortable polling:** unmount/new-upload/timeout all `AbortController.abort()`
  the in-flight GET; no setState-after-unmount.
- **Verified:** 34 Playwright UAT (incl. catalog-lag, same-name, padded-name,
  timeout) green; build clean. **Live:** upload → 202 → "Syncing…" → converged at
  **~22.7 s** (scannerd) with **no manual reload**, pending row cleared, table
  lists the chime, **0 console errors/warnings**, screenshots @375 + @1280.
- **Reviews:** two GPT-5.5 adversarial cycles reconciled (filename-trim,
  timeout-abort, mock-202 faithfulness fixed; same-name/same-size accepted as
  documented trade-off). **Deploy:** SPA-static-only (no Rust binary) under the
  hardware-test dead-man wrapper; snapshot `spa.b1-backup-20260615-175603`; wlan0
  + SSH intact; webd active; system `degraded` only from the benign stock
  `rpi-zram-writeback.timer` (pre-existing).

**Remaining for full §4.5/§1.1 parity:** mp3→WAV transcode + multi-file + 5 s/
normalize on upload; chime rename; event-driven scannerd rescan (systemic, would
shorten the ~22 s convergence across all categories); per-`job_id` status
endpoint.

---

## ⏯️ Resume here — CHIMES-ON-MEDIA + IMMEDIATE SET-ACTIVE LIVE (2026-06-15)

**Operator requirement (locked, now satisfied):** upload a lock chime → it lands
in a `Chimes/` folder ON the media drive → "Set Active" copies it to the media-drive
root as `LockChime.wav` so the car has it **immediately** (no manual unplug/replug).

✅ **Shipped + proven live on `cybertruckusb.local` (see `files/hw-results.md`
"Phase 2 — chimes-on-media"):**
- **Chime library moved into `media.img`** (`Chimes/` folder), off ext4
  `/data/teslausb/chimes`. webd `list_chime_library` reads the media catalog; the
  `/api/chimes/library/*` and legacy `/api/chime-scheduler/library/*` routes share one
  media-backed impl; the scheduler snapshot's `library` field is sourced from media.
- **Per-partition hot-handoff (gadgetd):** P2/media handoffs now apply **immediately
  by default** even while a USB host is enumerated (the car only *reads* that image);
  P1/TeslaCam still gated behind `--allow-hot-handoff` (C1/C2 unmeasured). The Phase-1
  bench stopgap `--allow-hot-handoff` flag was **removed** from `gadgetd-control.service`.
- **`install_file` ENOENT fix (gadgetd `mutate.rs`):** auto-create the destination's
  parent (`create_dir_all`) before the canonicalize jail check, so a first-ever install
  into a new category folder (`Chimes/`, `Wraps/`, …) on a fresh `media.img` no longer
  fails with `No such file or directory`. +2 regression tests.
- **End-to-end hardware proof:** `POST /api/chimes/library` (a minted PCM WAV) → job
  `done` in ~3 s → byte-identical at `Chimes/testchime.wav` on the RO media mount → it
  appears in `/api/chimes/library` + the scheduler snapshot `library` after one scan
  cycle (~15 s) → `POST …/testchime.wav/activate` → job `done` in ~3 s → media-root
  `LockChime.wav` updated **immediately** to the identical bytes (sha match), **no
  replug**. Playwright on the live `/media` page: Lock-Chimes table renders
  `testchime.wav — 86 KB — Valid` with Download/Set Active/Delete, schedule picker lists
  it, **0 console errors/warnings**, screenshots @375 + @1280.
- **Deploy:** 5 aarch64 binaries (gadgetd/webd/scannerd/indexd/schedulerd) installed
  under the hardware-test dead-man wrapper; GPT-5.5 deploy-plan review
  (PROCEED-WITH-CHANGES) reconciled — unit restored from verified backup (no blind
  `sed`), explicit rollback predefined, restart order = control-stop→gadgetd→control-start.
  All 6 services active/enabled, wlan0 + SSH intact, system `degraded` only from the
  benign stock `rpi-zram-writeback.timer`.

**Remaining for full §4.5/§1.1 parity:** mp3→WAV transcode + multi-file + 5 s/normalize
on upload; chime rename; **car-side pickup** of a `LockChime.wav` change (soft
medium-change vs. re-enumeration) is the §1.1 / C1 car-only verification.

---

## ⏯️ Resume here — REDEPLOY DONE: HEAD live on hardware, foundation complete (2026-06-16)

**Operator granted full live-hardware access and asked to finish fast + optimize
build/test.** The device had drifted **70 commits behind HEAD**; the highest-leverage
move was not new code but **rebuild HEAD + redeploy the stack**. ✅ **Done — the full
foundation (F1–F6) is now LIVE and verified on `cybertruckusb.local`.**

**What is now true on the device (all verified — see `files/hw-results.md`):**
- ✅ **F1 two-LUN foundation LIVE** — lun.0 → `teslacam.img` (`ro=0`, car records),
  lun.1 → `media.img`. UDC `configured`.
- ✅ **F2 enforced** — lun.1 `ro=1` (car can no longer write Media metadata) after
  the gadgetd recompose; lun.0 stays `ro=0`.
- ✅ **F3 + media seam LIVE** — `/run/teslausb/media-ro` mounted RO (loop0p1);
  `GET /api/media/content` serves real bytes (wrap PNG → 200/4940 B). Playwright
  device-smoke (14 routes × 2 viewports) **console-clean** — prior `/wraps` 503 gone.
- ✅ **F6** — HEAD scannerd + indexd serve the catalog over both images.
- ✅ **wifid crash-loop stopped** — `disable --now wifid` (reversible; NetworkManager
  owns wlan0). Only remaining failed unit is the benign stock `rpi-zram-writeback.timer`.
- ✅ **HEAD app stack deployed** — gadgetd/gadgetd-control/webd/scannerd/indexd/schedulerd
  all active on HEAD binaries; new SPA bundle served.
- ✅ **F5/F4 media WRITE path PROVEN LIVE on two LUNs** — a real `POST /api/wraps`
  round-tripped through the gadgetd eject-handoff into `media.img`; the RO mount was
  suspended/rebuilt around the mutate, `lun.0`/TeslaCam untouched, and the new wrap
  serves real bytes. **KEY GATE (by design):** media writes DEFER while a USB host is
  enumerated (`hot_handoff_unvalidated`) — production applies them at a COLD window
  (car ejects the drive) or with operator-opted `--allow-hot-handoff`. The car's
  mid-use eject tolerance is the **C1/C2** unknown that still needs the car.

**The optimized deploy loop (proven this session — use this, NOT `setup.sh deploy-app`):**
cross-build via podman (`build-release.sh --cross-podman --spa-project spa`, ~20 s warm)
→ `scp` binary to `/home/pi/teslausb-deploy/incoming/` → sha256-verify → backup current
to `.prev-<ts>` → `sudo install -m755` (unlink+create, ETXTBSY-safe) → restart one
service at a time → verify. For gadgetd specifically: **stop control+oneshot BEFORE
swapping the binary** (`gadgetd up` won't rewrite a bound gadget), then `start` to
recompose. `deploy-app` is UNSAFE here (it would start the running wifid + never
rebinds the gadget).

**Remaining (next):**
1. **C1/C2 (car accepts 2 LUNs + mid-use eject tolerance)** — the single make-or-break
   that needs the car. Frame a one-visit plan: confirm the car mounts both LUNs and
   records to TeslaCam, then measure whether a hot media-LUN eject (the `--allow-hot-handoff`
   path) disrupts recording — the gate that lets media uploads apply without waiting
   for the car to cycle the drive.
2. **Fixed wifid deploy (optional, deferred)** — only after reading
   `watchdog.rs`/`nmcli.rs`/`orchestrator.rs` `tick()` to PROVE empty-creds idle never
   resets the SDIO chip / seizes wlan0. Until then leave disabled (WiFi is fine).
3. **B-tier follow-up:** ✅ **DONE (2026-06-16)** — gadgetd `media_ro_*` health +
   `pending/applying_mutations` counts now flow through webd `map_gadget_status`
   (`gadget.rs`) to the SPA; MediaHub shows a **"Media mount (read)"** row.
   Live-verified on hardware: `/api/gadget/status` returns
   `media_ro_mounted:true, media_ro_path:/run/teslausb/media-ro` and the SPA row
   renders "Mounted (/run/teslausb/media-ro)" (Playwright /settings, console clean).
   `cargo test -p webd` 225 passed; media-hub UAT 14 passed; GPT-5.5-reviewed (no
   blocking). See `files/hw-results.md` "media_ro health passthrough".
4. **Continue the feature backlog** against the now-current device, parallelized by
   non-overlapping surface.

**Last committed:** `f005183` (local branch `mhackermsft/b1-clean`, **not pushed**).
This session's deploy + status/hw-results updates are committed (`235f693` foundation,
`13e28aa` F4/F5, `f005183` feature-verify). Continue committing each milestone; never push.

**Just done (2026-06-16):** (1) §4.9 wraps thumbnails + §4.5 active-chime player
**live-verified on hardware** (real decode — 2/2 wrap `<img>` `naturalWidth=600`;
chime `<audio>` `readyState=4` over `/api/media/content` [206]); (2) **B-tier item 3
SHIPPED + deployed** — `media_ro_*` health passthrough now live (webd + SPA redeployed
to the device; new "Media mount (read)" row renders real gadgetd data, console clean).
See `files/hw-results.md` "feature-verify" + "media_ro health passthrough".

**`feat-wrap-caps` — DONE (§4.9 wrap filename rule + ≤10 count cap).** `POST
/api/wraps` now rejects (a) a filename whose stem (excluding `.png`) is empty, >32
chars, or contains anything outside `[A-Za-z0-9_- space]` with `422 invalid_filename`,
and (b) a brand-new wrap once `Wraps/` holds 10 entries with `422 wraps_full` — both
BEFORE any gadgetd handoff. An exact re-upload of the same **destination `rel_path`**
is a replace and allowed even at capacity. The 512×512–1024×1024 dimension bound
(operator-confirmed) + PNG magic + ≤1 MB were already enforced. Backend-only
(`rust/crates/webd/src/{media_upload.rs,wraps.rs,tests.rs}`); error surfaces via the
SPA's existing generic upload-error banner → no SPA change. Verified by Opus: `cargo
test -p webd` = **221 passed, 0 failed**; 11 wrap tests; zero new clippy warnings
(only pre-existing scheduler.rs:37 / lib.rs:126 pass-by-value warnings).
- **GPT-5.5 review reconciled:** *Important* — the replace check first used the bare
  file `name`, but `list_wraps` returns every row under `Wraps/%` (incl. nested
  `Wraps/sub/<name>`), so a root-level upload could masquerade as a replace of a
  same-named nested file and bypass the cap → changed to exact full-`rel_path`
  comparison + added regression test `wraps_nested_same_name_is_not_a_replace_at_capacity`.
  GPT-5.5's optional suggestion to also filter the *count* to root-level only was
  declined: counting all `Wraps/%` rows is conservative (can only reject earlier,
  never bypass) and simpler. The shipped Boombox cap (`d480067`) had the same
  name-vs-`rel_path` pattern; it was fixed in the same way (`b7a4cae`'s follow-up
  commit) with its own `boombox_nested_same_name_is_not_a_replace_at_capacity`
  regression test for consistency.

**Next item to start:** open — and the genuinely-clean autonomous (non-hardware,
non-gated) backend lanes are now essentially exhausted. This session shipped the
last of the pure-logic validation lanes (Boombox + Wrap caps, both `cargo`-verified
and GPT-5.5-reviewed) and ticked §1 `TeslaTrackMode` recognition (scannerd logic
green). What remains in the list below is one of: (a) **live-hardware foundation**
(Phase 0 F1–F6, operator-run via `hardware-test`); (b) **gated backends** (SMB §2,
cloud sync §4.14, WiFi §4.16 — need their daemon serve loops); or (c) **new
full-stack features** that need a webd route **+ SPA screen + Playwright** (LightShow
"set active" §4.10:324, Tracked-plate list §4.9:344, Chime rename §4.5:278). Each (c)
is a multi-surface lane — pick ONE and run the full Opus→mai→GPT-5.5→Playwright loop.
`chimelib-to-img` (req #4) stays NOT autonomous (needs F5 write path + hardware).
Confirm direction with the operator before starting a gated/Tier-C migration.

**Earlier (committed `b1b9bc1`): `feat-media-audio` (§4.6/4.7/4.8
in-browser audio playback).** Native `<audio controls preload="none">` per row on
Music / Boombox / Light Shows, sourced from `GET /api/media/content?path=<rel>&v=<mtime>`
via a new `api.mediaContentUrl(path, version)` helper (and `activeChimeAudioUrl`
refactored to delegate to it, byte-identical). Light Shows renders a player only for
`.mp3`/`.wav` rows, not `.fseq`. mai implemented; Opus added GPT-5.5's required
"no content-fetch on render" assertion and fixed a visual regression (Light Shows
`table-layout:fixed` column widths rebalanced 25/15/30/30 for the new Play column —
caught by the desktop screenshot, the DOM test had passed). GPT-5.5 design check:
**PROCEED-WITH-CHANGES** (split audio-now/thumbnails-later endorsed; honesty hinges
on citing BOTH the Rust range tests and the Playwright wiring — done). Verified: tsc
clean; `npx playwright test music/boombox/light-shows` = **42 passed**, clean
console/network. Files: `spa/src/api/client.ts`, `spa/src/screens/{Music,Boombox,
LightShows}.tsx`, `spa/src/styles/{music,boombox,light-shows}.css`,
`spa/test/uat/{music,boombox,light-shows}.spec.ts`.

**Open follow-ups (logged in session SQL `todos`, not blocking):**
- `f3-followup-mount-perms`: harden the F3 RO mount (`ro,nodev,nosuid,noexec,
  gid=<group>,fmask=0137,dmask=0027`) + add `webd` to that group so it can read
  the root-created exFAT mount; consider true mountpoint detection for 503-vs-404
  (deploy/live concern, gated:F1+C1).
- `f3-followup-installfile-subdir`: mai's reverted `install_file` `create_dir_all`
  change — needs its own review, likely lands with F5 write-path.
- F4 read-drain stays deferred: GPT-5.5 confirmed a mid-stream mount teardown
  failing with EIO + client retry is acceptable — no read-lease required for now.

---

## Legend

- `[ ]` not done / not yet proven.
- `[x]` **done and tested-successful** (UI: Playwright gate green; device:
  hardware-test wrapper PASS; logic: unit/integration green).
- Tags after an item: **(proven)** verified on hardware/UAT · **(partial)** some
  sub-parts done, behavior not complete · **(stub)** scaffold exists, no live
  behavior · **(gated:X)** blocked on dependency X · **(C)** operator/hardware-only.

## Architecture invariants these items must never violate

1. TeslaCam `lun.0` is **never** disconnected; the car can always write. (One
   bounded, verified exception per Requirements §1.1: an explicit active-chime
   change triggers a brief full re-enumeration that detaches the whole device,
   gated on a health check that recording resumes — no *routine* action may
   disconnect `lun.0`.)
2. Recorded TeslaCam clips are readable for the map (not just the ext4 archive).
3. Media upload/delete may eject **`lun.1` only**, never gating `lun.0`.
4. **All** media (incl. the chime *library* + active `LockChime.wav`) lives in
   the images — never shadow-copied to SD.
5. Reads: media via gadgetd's **RO loop-mount of `media.img`**; live clips via
   raw `pread` (no mount of the car-written volume) — [`ADR-0003`](./adr/0003-media-read-path.md).

---

## Phase 0 — Foundation slice (live-hardware; the end-to-end backbone)

These six items (F1–F6) are the **live-device** foundation. **STATUS (2026-06-16):
the two-LUN foundation is LIVE and the read path is fully wired on the device.**
F1 (2-image migration), F2 (`lun.1 ro=1`), F3 (RO loop-mount + webd media seam),
and F6 (scannerd raw-`pread` + indexd catalog over both images) are **done and
verified on hardware** (HEAD stack deployed via the layered redeploy 2026-06-16 —
see `files/hw-results.md`). The device runs `lun.0=teslacam.img` (car-writable,
`ro=0`) + `lun.1=media.img` (`ro=1`), UDC `configured`, `/run/teslausb/media-ro`
mounted RO, and `GET /api/media/content` serves real bytes (Playwright device-smoke
console-clean across 14 routes × 2 viewports). F4/F5 remain gated (no `webd` reader
fds to drain yet / no live lun.1-only write proof). **C1 (does the car accept two
LUNs) is the single make-or-break that still needs the car.**

- [x] **F1 · 2-image migration on the live device** (single `disk.img` → `lun.0`
  `teslacam.img` + `lun.1` `media.img`). **DONE & LIVE** — the device runs the two
  single-partition images under `mass_storage.usb0` (`lun.0`→`teslacam.img`,
  `lun.1`→`media.img`), UDC `configured`, gadget attached; verified by on-device
  inventory 2026-06-16 (`files/hw-results.md`). Host enumerates exactly 2 drives.
- [x] **F2 · Enforce `lun.1 ro=1`** in gadgetd configfs so the car cannot write
  media exFAT metadata (makes the RO-mount sole-writer premise true — GPT-5.5 #9).
  **DONE & LIVE 2026-06-16** — HEAD gadgetd deployed; gadget recomposed (stop
  control+oneshot → install → `gadgetd up`); on-device configfs now reads
  `lun.0/ro=0` (car records) + **`lun.1/ro=1`**, UDC re-enumerated `configured`.
  Per-LUN `ro` in `config.rs`; `ro` set once at bring-up, persists across the
  eject-handoff. GPT-5.5-reviewed runbook (stop-before-swap; `up` won't rewrite a
  bound gadget). Evidence: `files/hw-results.md` (Layer 2).
- [x] **F3 · gadgetd RO loop-mount of `media.img`** — persistent, gadgetd-owned;
  exposes a media-root path (`/run/teslausb/media-ro`) for `webd` to read.
  **DONE & LIVE 2026-06-16** — `gadgetd serve` brought up the RO mount:
  `findmnt /run/teslausb/media-ro` → `exfat ro … /dev/loop0p1` (single loop, no
  double-mount). webd media seam serves real bytes on-device:
  `GET /api/media/content?path=Wraps/wrapfix-100523.png` → **200, 4940 B,
  image/png**; range request → **206**. Playwright device-smoke (14 routes × 2
  viewports) now **console-clean** — the prior `/wraps` 503 is resolved.
  `mediamount.rs`: `losetup -rfP` + `mount -o ro` (resolved via service PATH
  `/usr/sbin/losetup`,`/usr/bin/mount`), fail-closed `suspend`/`resume` around a P2
  RW mutate. Evidence: `files/hw-results.md` (Layer 2). **Follow-up (logged, B-tier,
  non-blocking):** webd `/api/gadget/status` does not yet surface the `media_ro_*`
  health fields gadgetd emits (`gadget.rs:357-374`) — wire them through for
  observability.
- [x] **F4 · Handoff read-drain / quiesce** — a read-lease so an in-flight media
  read is drained/blocked before a `lun.1` RW mutate; RO mount torn down and
  rebuilt around the handoff (GPT-5.5 #5). Extends the existing handoff state
  machine; "never two writers / never wrong bytes" outranks "always give the
  drive back". **RO-mount suspend/resume-around-mutate PROVEN LIVE 2026-06-16** —
  the F5 wrap-write handoff tore down `/run/teslausb/media-ro` and rebuilt it RO
  (single loop, no leak) around the mutate, with `lun.0` never touched
  (`files/hw-results.md`). **Remaining (B-tier, non-blocking):** draining in-flight
  `webd` reader fds — deferred until long-lived reader leases exist (today reads are
  short `std::fs` opens, nothing to drain).
- [x] **F5 · gadgetd eject-handoff write path (lun.1 only)** — install/delete via
  losetup→mount RW→mutate→sync→umount→re-present, cycling **only** `lun.1`.
  **MECHANISM PROVEN LIVE on two LUNs 2026-06-16** — `POST /api/wraps` (real
  PNG) → webd stages blob → `enqueue_mutation` IPC → gadgetd durable queue →
  `LoopMutator` eject-handoff applied it; `/api/wraps` then lists the new wrap,
  queue empties, **`lun.0`/TeslaCam stays `ro=0`/teslacam.img untouched**
  (partition=2 handoff only), `lun.1 ro=1`, UDC re-enumerated, seam serves the new
  bytes (200/2339B). **KEY GATE (by design):** the drain DEFERS while a USB host is
  enumerated (`hot_handoff_unvalidated`, handoff.rs:306) — production applies media
  writes only at a COLD window (car ejects the drive) OR with operator-opted
  `gadgetd serve --allow-hot-handoff`. Bench drain was validated by temporarily
  enabling that flag (reversible drop-in, dead-man-wrapped) then restored to
  production-safe. **Remaining gated:C1/C2** — measure the car's mid-use eject
  tolerance before enabling hot handoff in the car. Evidence: `files/hw-results.md`.
- [x] **F6 · scannerd raw `pread` reader + indexd catalog** for both images.
  **DONE & LIVE** — HEAD scannerd + indexd deployed to the device (Layer 1 redeploy
  2026-06-16); both read both single-partition images and the catalog serves real
  data: `/api/clips` 200 with buckets/timestamps, all 5 toybox listings + `/api/chimes`
  (installed `LockChime.wav`) 200. Verified on ARM hardware (`files/hw-results.md`).

---

## 1. Car-facing gadget & storage (`Requirements.md` §1)

- [x] Present as USB Mass Storage gadget (kernel `usb_f_mass_storage`, zero
  userspace in write path). **(proven on hardware)**
- [ ] **Two LUNs** (`lun.0` TeslaCam RW-by-car, `lun.1` Media RO-by-car) live on
  the device. **(partial: host/bench-proven; live device still single disk.img —
  see F1) (gated:C1)**
- [x] Tesla standard TeslaCam folder names (`RecentClips`/`SavedClips`/
  `SentryClips`) recognized; per-event subfolders + `event.json`/`thumb.png`;
  `<ts>-<camera>.mp4` parsing. **(proven: catalog + bench clips)**
- [x] `TeslaCam/TeslaTrackMode/` recognized in folder lists. **(DONE — scannerd
  `Bucket::from_path` maps `teslatrackmode`/`trackclips` → `TeslaTrackMode` (used by the
  producer at `produce.rs:97`); indexd + retentiond carry the same `FolderClass`. Logic
  green: `cargo test -p scannerd` = 80 passed incl. bucket roundtrip; retentiond
  `from_path("TeslaCam/TeslaTrackMode/…")` classification test passes.)**
- [ ] Media-drive root layout (`LockChime.wav`, `Boombox/`, `Music/`, `LightShow/`,
  `Chimes/`, `Wraps/`, `LicensePlate/`). **(partial: folder set + `Wraps/`
  root-folder fix proven on the single-`disk.img` device; the layout is not
  proven on a live `media.img` LUN, and `Chimes/`-in-image is still pending —
  see next item. gated:F1+F3)**
- [x] **Chime library `Chimes/` lives IN `media.img`** (moved off `/data/teslausb/chimes`). **(DONE 2026-06-15 — webd `list_chime_library` reads the media catalog `Chimes/*.wav`; `/api/chimes/library/*` + legacy `/api/chime-scheduler/library/*` share one media-backed impl; scheduler snapshot `library` sourced from media. LIVE-PROVEN: upload landed at `Chimes/testchime.wav` on the RO media mount, listed via `/api/chimes/library` + snapshot after a scan cycle. See `files/hw-results.md` "Phase 2".)**
- [ ] Configurable advertised capacity per drive (TeslaCam 64 GB / Media 32 GB
  defaults, 4–2048 GB), fully pre-allocated. **(see §4.11 resize) (gated:C1)**

### 1.1 Change propagation to the car

- [ ] **Soft SCSI medium-change** on `lun.1` after directory changes (new/deleted
  media) — car re-reads listings without re-plug; `lun.0` unaffected. **(port
  `tesla_cache_invalidate.sh` behavior into gadgetd)**
- [ ] **Full USB re-enumeration** ONLY for an active-`LockChime.wav` change, with a
  bounded health check that recording resumes. **(port `tesla_gadget_rebind.sh`)**
- [ ] **Hardware test:** confirm the car actually picks up directory changes via
  soft medium-change, and a chime change via re-enumeration (Requirements §1.1
  is a v1-observed behavior to re-verify on B-1). **(C)**

---

## 2. SMB / network shares (`Requirements.md` §2)

- [ ] `TeslaCam` + `Media` SMB shares published (browseable, read-write).
- [ ] Authenticated (guests rejected, `map to guest = Bad User`); no anonymous access.
- [ ] Set/change Samba password from the web UI (8–63 chars).
- [ ] Toggle Samba on/off from Settings; top-bar Samba dot reflects state.
- [ ] SMB reads/writes land in the correct folder; car re-reads on next medium-change
  (chime still needs re-enumeration). **(depends on §1.1)**
- [ ] **SMB delete/move** of files directly from Explorer/Finder (drag-out, delete)
  works on both shares (Requirements §2: shares are read-write). **(depends on §2 shares)**

---

## 3. Web UI — global shell (`Requirements.md` §3)

- [x] Single responsive SPA (mobile + desktop), reached at device host. **(proven)**
- [x] Top bar: brand→Map, theme toggle (persisted). **(proven)**
- [ ] System-health status dot polling `/api/system/health` (green/amber/red/grey;
  click→Settings health). **(partial: thin health data — see §4.12/A5)**
- [ ] Samba status dot (shown only when sharing on). **(depends on §2)**
- [ ] Primary nav (sidebar desktop / bottom tabs mobile), availability-gated items. **(partial: nav present; per-feature availability gating to finish — A9)**
- [ ] Feedback model: JSON for AJAX + flash banners; live-poll views. **(partial: proven on media routes; not yet audited across all routes — see §5 error-code audit)**

---

## 4. Web UI pages

### 4.1 Trip Map (home) — `Requirements.md` §4.1

- [x] Day routes as speed-colored polylines + speed legend; SEI-derived GPS. **(proven)**
- [x] Day card stats (distance, duration, trips, events, avg/max speed). **(A4 proven)**
- [x] Prev/next day in one fetch. **(proven)**
- [x] Event markers by severity; click → open footage. **(A6/A6b proven)**
- [ ] Filters: date range, **map bbox (pan/zoom)**, event type, severity, min distance. **(partial: verify bbox + all filters)**
- [x] Side panel tabs (Events / Trips / All Clips) + source folder switch. **(proven;
  **All Clips tab LIVE-verified on hardware 2026-06-16** — the panel rendered all 4
  real device clips from the live catalog with correct `folder_class` labels
  [RecentClips/SavedClips/SentryClips], angle counts, sentry flags + timestamps;
  no doomed `/clips/*/stream` request for the `ro_usb` clips; console clean. See
  `files/hw-results.md` "clips browse live".)**
- [ ] Units & timezone preferences re-render speeds/times. **(partial: wire to Settings §4.15)**

### 4.2 Event / Video Player — `Requirements.md` §4.2

- [x] Stream **archived** clip with HTTP range (seek). **(proven for archived clips;
  live clips are the separate item below)**
- [ ] **Play live (not-yet-archived) recorded clips** on the map. **(gated:B1 archive
  loop, then the lun.0 `ReadFile` fallback — ADR-0003 / `contracts/scannerd-readfile.md`.
  **Gate confirmed live 2026-06-16:** the device's clips are all `view_kind=ro_usb`;
  `GET /api/clips/1/stream` correctly `404`s and EventPlayer shows the "Not yet
  archived" overlay [`data-testid=video-unarchived`] by design — playback genuinely
  needs the B1 archive pipeline to produce a `VIEW_ARCHIVE` angle.)**
- [x] Switch camera angle (position preserved where possible). **(proven)**
- [x] Navigate clips within an event (prev/next). **(A6b proven)**
- [ ] Telemetry HUD overlay (SEI: speed/gear/brake/throttle/steering/AP-FSD), synced. **(partial: client-side SEI parse exists — A7; verify full HUD)**
- [ ] Download single angle + download whole event as ZIP. **(verify ZIP path)**
- [ ] Archive event to cloud. **(gated:B3)**
- [ ] Delete event/clip (confirm) via privileged path-validated helper; car re-reads. **(partial: TeslaCam delete via handoff — verify end-to-end)**

### 4.3 Analytics — `Requirements.md` §4.3

- [ ] Storage usage per partition (TeslaCam/Media/SD). **(partial: SD/IO richness — A5)**
- [x] Video stats + per-folder breakdown. **(A4 proven)**
- [ ] Storage-health summary with alerts/recommendations. **(gated:B1)**
- [ ] Recording-time estimate (confidence-labeled). **(partial)**
- [x] Driving stats & charts (distance/time, counts, avg/max, FSD %, events/100mi,
  severity & FSD timelines). **(A4 proven)**
- [x] Empty-state when index not ready. **(proven)**

### 4.4 Media hub — `Requirements.md` §4.4

- [x] Landing cards to Chimes/Music/Boombox/LightShows/Wraps/Plates, availability-gated. **(proven)**

### 4.5 Lock Chimes — `Requirements.md` §4.5  ← **highest current divergence**

- [x] **Active chime card done right (v1-faithful):** filename (`LockChime.wav`)
  + size + a native `<audio>` **player** for the real active sound; **no Remove
  button** (dead remove modal/handlers/CSS removed). No provenance/original-name
  or duration — v1 shows neither (per captured baseline DOM
  `docs/tasks/parity-baseline/lock-chimes/`). **(DONE — player sourced from `GET
  /api/media/content?path=LockChime.wav` cache-busted by mtime; mai impl,
  GPT-5.5 Approve, verified `npx playwright test media.spec.ts` = 22 passed
  [clean console, 375 + 1280, nav ~147 ms] + tsc clean. **LIVE-PROVEN on hardware
  2026-06-16:** the `<audio>` element on `/media` fetched `LockChime.wav` from the
  device's RO media mount via `/api/media/content` [206], decoded to
  `readyState=4` [HAVE_ENOUGH_DATA], duration 2.49 s, console clean — see
  `files/hw-results.md` "feature-verify". Real in-browser playback works.)**
- [x] Play any library chime in-browser. **(DONE — the chime-library table on `/media`
  renders a native `<audio data-testid="library-audio" preload="none">` per row sourced
  from `GET /api/chimes/library/{name}/audio`. **As of 2026-06-15 the library is
  media-backed** (`Chimes/` in `media.img`, not the old ext4 `/data/teslausb/chimes`);
  the legacy `/api/chime-scheduler/library/.../audio` alias still resolves to the same
  media-backed handler. LIVE-PROVEN 2026-06-15: `testchime.wav` listed from the media
  catalog and its row rendered with a Valid badge + Download/Set Active/Delete on the live
  `/media` page, console clean. See `files/hw-results.md` "Phase 2".)**
- [ ] Upload chime(s) `.wav` (+`.mp3`→WAV), ≤1 MB & ≤5 s; added to `Chimes/`. **(partial:
  single-file `.wav` install into `media.img` `Chimes/` LIVE-PROVEN 2026-06-15 [job done
  ~3 s, byte-identical on the RO media mount; `files/hw-results.md` "Phase 2"]; multi-file
  + mp3 transcode + 5 s/normalize still to verify)**
- [ ] Delete library chime. **(partial: delete path exists; verify against `Chimes/` in image)**
- [ ] Rename a chime (v1 rename API). **(not started)**
- [x] **Set active** → copy library file to media-root `LockChime.wav`, applied
  **immediately** (per-partition hot-handoff on P2; no manual replug); UI shows which
  library chime is active. **(DONE 2026-06-15 bench-proven — `POST …/library/{name}/activate`
  re-validates the WAV and installs it to media-root `LockChime.wav` via the gadgetd
  queue; the new gadgetd applies P2 handoffs immediately while the host is enumerated.
  LIVE: media-root `LockChime.wav` updated byte-identical to the activated chime in ~3 s,
  no replug — `files/hw-results.md` "Phase 2". Car-side pickup (soft medium-change vs.
  full re-enumeration) is the remaining §1.1 / C1 car-only check.)**
- [ ] Groups (create/edit/delete; persist `chime_groups.json`). **(partial: A3 UI +
  schedulerd render proven; verify CRUD round-trip)**
- [ ] Schedules (weekly/date/holiday/recurring; CRUD+enable; `chime_schedules.json`). **(partial:
  A3 rule engine + schedulerd serve + UI proven; **enforcement loop** A3d gated:F4)**
- [ ] Random mode from a group (`chime_random_config.json`); rotates active chime. **(partial: model exists; enforcement gated:F4)**
- [ ] **Enforcement loop** (per-minute: swap `LockChime.wav` when a rule fires via a
  gadgetd handoff + re-enumeration). **(A3d, gated:F4 + §1.1)**

### 4.6 Music — `Requirements.md` §4.6

- [ ] Browse library incl. nested folders. **(partial: flat list proven; nested folder browse to verify)**
- [x] Play track in-browser (native `<audio preload="none">` per row, streamed from
  `GET /api/media/content?path=<rel>&v=<mtime>`). **(DONE — read path covered by
  webd range-streaming integration tests; SPA wiring verified `npx playwright test
  music.spec.ts` incl. mocked-list player test asserting preload=none + encoded src +
  cache-bust + NO content-fetch on render; 14 passed, clean console/network)**
- [ ] Upload `.mp3/.flac/.wav/.aac/.m4a`, up to **2 GB**, **16 MB chunked** upload. **(gated: chunked-upload backend — Tier-C remainder A1/A2)**
- [ ] Create folders + move files between folders. **(gated: music folder ops — A1)**
- [ ] Delete files (and folders). **(partial: bulk delete proven; folder delete to verify)**
- [x] Media-drive storage usage (used/free/total). **(proven)**

### 4.7 Boombox — `Requirements.md` §4.7

- [x] List/play current sounds. **(DONE — list proven; native `<audio preload="none">`
  per row from `GET /api/media/content`; verified `npx playwright test boombox.spec.ts`
  incl. mocked-list player test [preload=none + encoded src + no content-fetch on render];
  14 passed, clean console/network. Read path covered by webd range-streaming tests.)**
- [x] Upload `.mp3/.wav`, ≤1 MB each, ≤5 files total (clear rejection). **(DONE —
  size cap (`422 file_too_large`, 1 MiB) + ≤5-files-total cap (`422 boombox_full`,
  pre-handoff, exact-name replace allowed) verified by `cargo test -p webd` 215 passed;
  9 boombox tests incl. off-by-one + case-variant guards. Concurrency TOCTOU accepted
  as documented single-operator limitation. Successful-install path proven earlier.)**
- [x] Delete (incl. bulk). **(bulk-delete A2 proven)**

### 4.8 Light Shows — `Requirements.md` §4.8

- [x] List shows grouped by name stem (`.fseq` + paired audio). **(proven)**
- [x] Play show audio in-browser. **(DONE — native `<audio preload="none">` rendered
  ONLY for audio rows [`.mp3`/`.wav`], not `.fseq`, from `GET /api/media/content`;
  verified `npx playwright test light-shows.spec.ts` incl. mocked-list test asserting
  exactly one player [`.fseq` has none] + no content-fetch on render; 14 passed.
  Table column widths rebalanced for the new Play column [visual gate].)**
- [ ] Upload `.fseq`/audio single ≤100 MB, or **ZIP ≤500 MB** auto-extracted+flattened. **(gated: ZIP upload backend — Tier-C A1)**
- [ ] Set active show (`lightshow_active.json`). **(not started)**
- [x] Delete files/shows (incl. bulk). **(bulk-delete proven)**

### 4.9 Wraps & License Plates — `Requirements.md` §4.9

- [x] **Wraps:** list with raw-PNG thumbnails. **(DONE — SPA `<img>` Preview column wired via
  `api.mediaContentUrl(rel_path, modified)`; UAT seeds `WEBD_MEDIA_RO_ROOT` + asserts real decode
  `naturalWidth>0`; `npx playwright test wraps.spec.ts` green + populated desktop screenshot verified.
  **LIVE-PROVEN on hardware 2026-06-16:** both wraps on the device's `/wraps` page decoded real
  bytes [2/2 `<img>` `naturalWidth`=600, `complete`] served from `media.img` via the RO mount seam
  [200], console clean — see `files/hw-results.md` "feature-verify".)**
- [x] Wrap upload: `.png` only, ≤1 MB, 512×512–1024×1024, name ≤32 `[A-Za-z0-9_- space]`,
  ~10 max; atomic publish. **(DONE — `validate_wrap_filename` (≤32-char stem, charset
  `[A-Za-z0-9_- space]`) + `WRAPS_MAX_FILES=10` count cap with exact `rel_path` replace
  exception, both rejecting `422` pre-handoff; PNG magic + 512–1024 dimension + ≤1 MB still
  enforced. `cargo test -p webd` = 222 passed incl. 11 wrap tests; GPT-5.5-reviewed: replace
  identity fixed from bare name → full `rel_path` so a nested same-named file can't bypass the
  cap, regression test added. **Now LIVE-PROVEN on hardware 2026-06-16:** a real
  `POST /api/wraps` round-tripped through the gadgetd eject-handoff into `media.img`
  on the two-LUN device and the new thumbnail serves real bytes — see F5 /
  `files/hw-results.md`.)**
- [x] Wrap delete (incl. bulk). **(proven)**
- [ ] **Plates (images):** list w/ thumbnails, upload, delete `.png` ≤512 KB,
  exactly 420×75 (NA)/492×75 (EU), name ≤12 alnum, ≤5. **(partial: validation A1 done;
  thumbnail Preview column DONE — `<img>` wired + Playwright `naturalWidth>0` (license-plates.spec.ts);
  upload caps + cropper deferred — A2)**
- [ ] **Tracked-plate list (privacy/redaction):** add/edit/delete (uppercase ≤16,
  label ≤64, notes ≤240, dedupe), bulk delete, redaction toggle. **(not started)**

### 4.10 Cloud Archive — `Requirements.md` §4.10

- [ ] Configure cloud backend (rclone family: S3/B2/Drive/Dropbox/Crypt…). **(gated:B3 + C5 security ruling)**
- [ ] Choose sync folders + priority; sync-non-event-media; sync-telemetry-RecentClips. **(gated:B3)**
- [ ] Bandwidth limit (kbps) + cloud free-space reserve. **(gated:B3 + C2 TX-cap)**
- [ ] Max retries → dead-letter; auto-cleanup; keep-until-synced. **(gated:B3)**
- [ ] Trigger immediate sync / stop in-progress. **(gated:B3)**
- [ ] Live progress (status, queue pending/synced, recent history, polled). **(gated:B3)**
- [ ] `uploadd serve` loop is a **stub** today. **(stub → B3)**

### 4.11 Storage Settings — `Requirements.md` §4.11

- [ ] Set TeslaCam + Media drive sizes (GB 4–2048) → resize backing image +
  brief re-advertise (~30–60 s); reject shrink below usage. **(gated:B5 + C1)**
- [ ] Safety buffer (≥5 GB) protecting OS partition. **(gated:B5)**
- [ ] Cleanup tuning: target free %, Sentry max-age, preserve-GPS-clips. **(gated:B1/B2)**

### 4.12 Storage Health — `Requirements.md` §4.12

- [ ] View health: mount status, FS error counts, SMART/health severity, alerts. **(partial:
  Linux/Pi-gated probes — A5)**
- [ ] Online read-only `e2fsck` with fast-poll result. **(not started)**
- [ ] Arm/cancel fsck on next boot. **(not started)**
- [ ] Reboot device now (confirm). **(not started; gadgetd-gated reboot policy)**

### 4.13 Wi-Fi / Captive Portal — `Requirements.md` §4.13

- [ ] First-run setup AP (`TeslaUSB-Setup`, auto passphrase) + captive-portal
  redirect (Apple/Android/Windows/generic). **(gated:B4 + WiFi-invariant C)**
- [ ] Scan networks + saved networks w/ signal status. **(gated:B4)**
- [ ] Join network (SSID+pass; open needs none). **(gated:B4)**
- [ ] Disconnect / forget network. **(gated:B4)**
- [ ] Enable/disable setup AP + auto-restore timer (never lock out). **(gated:B4)**
- [ ] `wifid` UDS + webd wifi routes. **(wifid has AdminCommand core; serve/UDS missing — B4)**

### 4.14 Failed Jobs — `Requirements.md` §4.14

- [ ] View failed-job counts by subsystem (indexer, cloud) + paginated rows + reason. **(partial: webd-local SSE only — A8)**
- [ ] Retry selected. **(gated:B7 real gadgetd, not mocked)**
- [ ] Delete selected. **(partial — A8)**

### 4.15 Settings (system) — `Requirements.md` §4.15

- [ ] Toggle Samba + status dot. **(depends on §2)**
- [ ] Set/change Samba password (8–63). **(depends on §2)**
- [ ] Map/display prefs (units, timezone) + network settings. **(partial: GET-only today)**
- [ ] System-health card (per-subsystem behind top-bar dot). **(gated:A5/B1)**

---

## 5. Cross-cutting behavior (`Requirements.md` §5)

- [ ] **All media in the images, never on SD.** **(architecture-locked, but NOT
  yet true on disk: chime library still on `/data` and the RO-mount read path
  isn't built — closes when F3 + chime-lib move land)**
- [x] **Atomic writes** (temp→fsync→validate→rename, car-readable perms). **(proven:
  gadgetd `install_file` atomic; `create_dir_all` parent fix proven on hw)**
- [ ] **Filename safety** (reject `/`, `..`, NUL; per-category rules; reject symlinks). **(partial:
  jail proven in gadgetd; per-category rules — verify each)**
- [ ] **Validation/error codes:** 413 oversize, 400 bad, 404 missing, 409 dup, 500
  IO, 503 not-impl; flash+redirect for forms, JSON for AJAX. **(partial: audit each route)**
- [ ] **Change propagation** (soft medium-change for dirs; re-enumeration for chime),
  never stalling TeslaCam. **(see §1.1)**

---

## 6. B-1 binding requirements + hardware gates (`Requirements.md` §6)

- [ ] **#1** TeslaCam never disconnected. **(partial: media handoff cycles lun.1
  only on bench; NOT proven on the live device, which is still single-LUN —
  gated:F1+C1)**
- [ ] **#2** Recorded (live) TeslaCam clips readable for the map. **(gated:B1 then lun.0 ReadFile)**
- [ ] **#3** Media upload/delete ejects lun.1 only. **(partial: bench-proven;
  gated:F1+C1 for the live second LUN, and F4 read-drain)**
- [ ] **#4** All media in images incl. chime library. **(gated:F3 + chime-lib move)**
- [ ] **#5** Reproduce every v1 capability + look-and-feel, lower CPU/I/O/mem, zero clip loss. **(the whole list above)**

### Tier-C hardware/operator gates (block multiple items; cannot be done autonomously)

- [ ] **C1 · 2.1 LUN-acceptance vehicle spike** — does the car accept a SECOND
  read-only media LUN? Make-or-break; unblocks all calibration + F1. **(C)**
- [ ] **C2 · WiFi TX-cap (2.6) + governor defaults (2.7)** at-vehicle. **(C)**
- [ ] **C3 · Real Tesla footage validation** (replace synthetic SMPTE). **(C)**
- [ ] **C4 · Push held commits + port-80 + live deploy.** **(C)**
- [ ] **C5 · Security ruling on rclone-key write exposure** (blocks B3 config-write). **(C)**
- [ ] **C6 · Car change-propagation verification** (§1.1 soft vs full re-enum). **(C)**

---

## Recommended build order (folds in the review guidance)

> **C1 is make-or-break and comes FIRST.** The entire two-image / `lun.1`-media
> direction (and therefore F1 migration, the RO `media.img` mount, and every
> "lun.1-only" safety claim) is invalid if the car won't accept a second
> read-only LUN. Run the C1 vehicle spike up front. Bench development of the
> Phase-0 slice (F2–F4) and the Lock-Chimes UI can proceed **in parallel** on the
> bench, but nothing media may be marked proven on the live device until C1 + F1
> pass.

0. **C1 · 2.1 LUN-acceptance vehicle spike (FIRST, blocking the direction).**
   If PASS → proceed to F1 migration. If FAIL → re-open the media-LUN
   architecture before building further.
1. **Phase 0 foundation slice** (F2→F3→F4) — `lun.1 ro=1` + RO media mount +
   handoff read-drain. Develop on the bench in parallel with C1; lands live after
   F1. Unlocks every media *read* (active-chime player, library/music/boombox/
   lightshow playback, wrap/plate thumbnails) with simple `std::fs`.
2. **Fix Lock Chimes page** (§4.5 active card + library playback) — first visible
   win on the new read path; highest current divergence from v1.
3. **retentiond serve loop (B1)** → archive RecentClips → unblocks live-clip map
   playback (§4.2 #2) and storage-health/analytics alerts.
4. **lun.0 `ReadFile` fallback** (only the not-yet-archived window) — small, per
   the simplified [`contracts/scannerd-readfile.md`](./specs/contracts/scannerd-readfile.md).
5. **Media write parity remainder** (chunked/multi upload, music folder ops,
   lightshow ZIP, plate cropper) — needs the multi-file gadgetd op.
6. **SMB (§2) + Settings (§4.15) + Storage Settings (§4.11/B5).**
7. **uploadd/cloud (B3, gated C5) · wifid/captive portal (B4, WiFi-gated) ·
   chime enforcement loop (A3d) · Failed Jobs richness (A8/B7).**
8. **Remaining Tier-C at-vehicle (after C1):** migration F1 → calibration C2 →
   real-footage C3 → change-propagation C6.

> Update this file as the source of truth: tick a box only after a tested-successful
> run, and link the evidence (Playwright report / `files/hw-results.md` entry).
