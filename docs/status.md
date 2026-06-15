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

## ⏯️ Resume here — in-flight work (2026-06-15 19:05 ET)

**Last committed:** `ae10a3b` (local branch `mhackermsft/b1-clean`, **not pushed**).
Latest working-tree change (uncommitted as this note is written, committed alongside
it): `feat-wrap-caps` (§4.9 Wrap name + count caps). Prior commits: `ae10a3b`
(docs: wrap dimension bound → 512×512–1024×1024), `d480067` (§4.7 Boombox ≤5-file
cap), `3dbf452` (§4.9 Wraps/Plates `<img>` thumbnails), `b1b9bc1` (§4.6/4.7/4.8
in-browser audio), `687579c` (chime-library hardening).

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

**Next item to start:** open. `chimelib-to-img` (move chime library into `media.img`,
requirement #4) is NOT cleanly autonomous — its acceptance needs the gadgetd lun.1
eject-handoff write path (F5-gated) + a hardware deploy (Tier-C), and it retargets
library serving to the `/api/media/content` seam (so wiring the current library
endpoint now is rework). Music §4.6 nested-folder browse is deferred (read-only tree
would be reworked when gated folder write-ops §4.6:290 land — do them together).
Remaining clean autonomous lanes are thin; candidates: §4.9 Tracked-plate list
(privacy/redaction, not started, larger new feature); the Boombox `rel_path`
follow-up above. Confirm with operator before starting a gated/Tier-C migration.

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

## Phase 0 — Foundation slice (DO FIRST; everything below depends on it)

Sequenced as a single vertical slice per the GPT-5.5/mai reviews (do **not** wire
every daemon at once). Get reads + a safe handoff lock proven before feature work.

- [ ] **F1 · 2-image migration on the live device** (single `disk.img` → `lun.0`
  `teslacam.img` + `lun.1` `media.img`). Runbook exists
  ([`usb-io-and-archiving-architecture.md`](./specs/usb-io-and-archiving-architecture.md) §6);
  host-built/bench-validated, **not yet run on the live device**. **(C, gated:C1 spike)**
- [ ] **F2 · Enforce `lun.1 ro=1`** in gadgetd configfs so the car cannot write
  media exFAT metadata (makes the RO-mount sole-writer premise true — GPT-5.5 #9).
  **(planner + unit tests DONE & bench-green 2026-06-12: per-LUN `ro` in
  `config.rs` — `lun.0` rw so the car keeps recording, `lun.1 ro=1`; `cargo test
  -p gadgetd` 76 passed in podman. `ro` is set once at bring-up and persists across
  the eject-handoff. Live enforcement takes effect at the next gadget bring-up, so
  it lands with F1 migration — gated:F1+C1. Follow-ups logged from GPT-5.5 review:
  startup `ro` verify/log, optional CLI override for the C1 spike, and exFAT
  integrity/repair on `media.img` is now solely Pi-side.)**
- [ ] **F3 · gadgetd RO loop-mount of `media.img`** — persistent, gadgetd-owned;
  exposes a media-root path (`/run/teslausb/media-ro`) for `webd` to read via
  `std::fs`. **(impl + unit tests DONE & bench-green 2026-06-12: new
  `mediamount.rs` (`losetup -rfP` + `mount -o ro`, idempotent `ensure_mounted`,
  fail-closed `suspend`/`resume` that refuse to stack on an image with a live
  loop — never-double-mount); a `ReadMountGate` trait injected into
  `run_handoff` suspends the RO mount before a P2 RW mutate and resumes after
  re-present (P1/TeslaCam never touched); suspend-fail ⇒ `Refused` before eject,
  resume-fail ⇒ degraded (surfaced via new `gadget_status` `media_ro_*` fields),
  not a fault. Reviewed by GPT-5.5 — its Critical (startup double-mount after a
  failed cleanup) is fixed by the loop-presence guard. `cargo test -p gadgetd
  --bins` 82 passed + clippy clean in podman. Full mount lifecycle needs real
  loop devices, and the `webd` read handlers that consume the path are separate
  gated items, so live enforcement lands with F1 — gated:F1+C1.)**
- [ ] **F4 · Handoff read-drain / quiesce** — a read-lease so an in-flight media
  read is drained/blocked before a `lun.1` RW mutate; RO mount torn down and
  rebuilt around the handoff (GPT-5.5 #5). Extends the existing handoff state
  machine; "never two writers / never wrong bytes" outranks "always give the
  drive back". **(partial: the RO-mount suspend/resume-around-mutate half landed
  with F3 — the regression-prevention piece. The remaining work, draining
  in-flight `webd` reader fds, is deferred until the `webd` media read handlers
  exist — there are no readers to drain yet.)**
- [ ] **F5 · gadgetd eject-handoff write path (lun.1 only)** — install/delete via
  losetup→mount RW→mutate→sync→umount→re-present, cycling **only** `lun.1`.
  **(partial: handoff mechanism + atomic install + `create_dir_all` parent fix
  proven on the live single-`disk.img` device and on 2-LUN bench; but
  genuine "lun.1-only while lun.0 stays up" is NOT live-proven — live device is
  still single-LUN (F1) — and the ADR-0003 read-drain/RO-remount around the
  write (F4) is not built. gated:F1+F4)**
- [ ] **F6 · scannerd raw `pread` reader + indexd catalog** for both images.
  **(reader + catalog logic proven on ARM bench: clips + 8 media_entries
  cataloged, bench re-validation PASS; NOT yet proven against both LUNs on the
  live device, which still runs single `disk.img`. gated:F1 for the live
  two-image proof)**

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
- [ ] `TeslaCam/TeslaTrackMode/` recognized in folder lists. **(verify in scannerd
  path allowlist)**
- [ ] Media-drive root layout (`LockChime.wav`, `Boombox/`, `Music/`, `LightShow/`,
  `Chimes/`, `Wraps/`, `LicensePlate/`). **(partial: folder set + `Wraps/`
  root-folder fix proven on the single-`disk.img` device; the layout is not
  proven on a live `media.img` LUN, and `Chimes/`-in-image is still pending —
  see next item. gated:F1+F3)**
- [ ] **Chime library `Chimes/` lives IN `media.img`** (moved off `/data/teslausb/chimes`). **(gated:F3)**
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
- [x] Side panel tabs (Events / Trips / All Clips) + source folder switch. **(proven)**
- [ ] Units & timezone preferences re-render speeds/times. **(partial: wire to Settings §4.15)**

### 4.2 Event / Video Player — `Requirements.md` §4.2

- [x] Stream **archived** clip with HTTP range (seek). **(proven for archived clips;
  live clips are the separate item below)**
- [ ] **Play live (not-yet-archived) recorded clips** on the map. **(gated:B1 archive
  loop, then the lun.0 `ReadFile` fallback — ADR-0003 / `contracts/scannerd-readfile.md`)**
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
  [clean console, 375 + 1280, nav ~147 ms] + tsc clean)**
- [ ] Play any library chime in-browser (streamed from image via RO mount). **(read
  path READY — `/api/media/content`, backend verified; needs SPA `<audio>` wiring + Playwright)**
- [ ] Upload chime(s) `.wav` (+`.mp3`→WAV), ≤1 MB & ≤5 s; added to `Chimes/`. **(partial:
  single-file install proven on hw; multi-file + mp3 transcode + 5 s/normalize to verify)**
- [ ] Delete library chime. **(partial: delete path exists; verify against `Chimes/` in image)**
- [ ] Rename a chime (v1 rename API). **(not started)**
- [ ] **Set active** → copy library file to `LockChime.wav` + **full re-enumeration**;
  UI shows which library chime is active. **(gated:§1.1 re-enumeration + F3)**
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
  `naturalWidth>0`; `npx playwright test wraps.spec.ts` green + populated desktop screenshot verified)**
- [x] Wrap upload: `.png` only, ≤1 MB, 512×512–1024×1024, name ≤32 `[A-Za-z0-9_- space]`,
  ~10 max; atomic publish. **(DONE — `validate_wrap_filename` (≤32-char stem, charset
  `[A-Za-z0-9_- space]`) + `WRAPS_MAX_FILES=10` count cap with exact `rel_path` replace
  exception, both rejecting `422` pre-handoff; PNG magic + 512–1024 dimension + ≤1 MB still
  enforced. `cargo test -p webd` = 221 passed incl. 11 wrap tests; GPT-5.5-reviewed: replace
  identity fixed from bare name → full `rel_path` so a nested same-named file can't bypass the
  cap, regression test added)**
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
