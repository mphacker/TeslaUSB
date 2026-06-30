# TeslaUSB B-1 — Build Status (vs. `Requirements.md`)

> ## ⏯️ RESUME HERE (2026-06-29) — indexd parse-livelock FIXED + deployed; clip GPS-parse robustness is the next item
>
> **Symptom (operator):** a real home→McDonald's→home drive showed only *part* of
> the route on the Trip Map. **Root cause (Tier-3, root-caused live + GPT-5.5
> concurrence):** `scannerd produce.rs` answered each `indexd` resync by
> SEI-walking the **entire** stable backlog (~340 clips) in one batch, blowing past
> indexd's 60 s `read_batch` timeout → EAGAIN (`os error 11`) → indexd drops +
> reconnects → re-arms resync → scannerd re-parses from scratch → permanent
> livelock; the catalog never finished, so most clips never got `ended_at`/trips.
>
> - [x] **`fu-parse-livelock` — chunked-produce cap (DONE + deployed + live-proven 2026-06-29).**
>   Cap expensive front SEI-walks at `MAX_FRONT_SHAPES_PER_BATCH=8`/pass, defer the
>   rest via new `stability.rs::unmark_emitted`, set the produce batch `complete=false`
>   while a front backlog remains (indexd consumer unchanged — already chunk-safe).
>   scannerd 104 lib + 9 main tests pass, clippy clean; GPT-5.5 adversarial review
>   (APPROVE-WITH-NITS, nit fixed: defer fronts only). Cross-built aarch64 (sha
>   `6c3cff4e…`), deployed under the hardware-test skill (dead-man rail, atomic swap,
>   catalog backup, rollback script). **Live result:** EAGAIN loop GONE, backlog of
>   ~343 clips drained, pruning runs (`complete=true`), load avg 2.1→0.75, device
>   healthy, recording undisturbed. Evidence: `files/hw-results.md`.
>
> - [ ] **`fu-clip-parse-state` — honest front parse-state + prompt-read robustness (NEXT, Tier-3).**
>   Removing the livelock exposed a **pre-existing** issue: the drive's RecentClips
>   yielded **0 GPS waypoints**, so they never formed trips. Decisive evidence: the
>   SavedClips segment at the stop (12:17–12:26) parsed with full GPS, but the pure
>   ephemeral RecentClips of the same drive (normal recorded sizes ~52 MB) yielded 0
>   waypoints. Because the livelock delayed parsing by *hours* and RecentClips is a
>   rolling buffer the car recycles, scannerd read those clips **after the car
>   overwrote the clusters** → no SEI/GPS → `duration_s=0`, `ended_at=NULL`. This
>   drive's GPS is unrecoverable; the fix prevents recurrence. GPT-5.5's structural
>   finding (adopt): `ended_at IS NOT NULL` conflates "not-parsed" with "parsed,
>   no-GPS," and `walk_clip_waypoints(...).ok()` swallows read/parse errors → no
>   visibility, infinite silent retry. **Design:** add a per-front parse-state
>   (`not_attempted` / `parsed_with_waypoints` / `no_waypoints` / `parse_error` /
>   `unplaceable`) keyed by stable fingerprint + parser version; prioritize
>   `not_attempted`; never re-walk `no_waypoints` unless fingerprint/version changes;
>   stop swallowing errors. **Validation:** confirm a fresh post-fix drive parses
>   promptly (the livelock masked the baseline — needs an operator test drive).
>
> ## ⏯️ RESUME HERE (2026-06-25) — Live-clip playback COMPLETE end-to-end; Map V1-parity shipped; Phase-2 deleter still deferred
>
> **§4.2 "live-clip map playback" is now fully closed — both paths proven on real
> hardware.** Two independent routes get a not-yet-archived clip to the player:
> 1. **Archive-then-play** (Phase-1 archive loop) — PROVEN LIVE on real footage
>    2026-06-23 (C3 gate): real RecentClips archived → webd 206 → Chromium decode
>    2896×1876, 0 dropped frames. retentiond `moov`/decodability gate DONE; the
>    earlier synthetic-stub decode block is resolved.
> 2. **Direct-from-USB streaming** — DONE + live-verified + committed/pushed
>    2026-06-25 (`2e0db7c`). webd `media.rs` falls back to a slot-0 `ReadFile`
>    loop for a non-archive (`ro_usb`/`live`) angle (new `read_client.rs`), archive
>    always preferred. Hardened with a **first-read stable-size gate** (both
>    `readable_size` & `total_size` must equal catalog `angles.size_bytes` → else
>    410; NULL/≤0 → fail-closed 404; per-request identity fence governs later
>    chunks). 2 gpt-5.5 review cycles. Gates: podman webd 306 + UAT trip-map 66/66
>    & event-player 38/38. Live (`cybertruckusb.local`, in-car, Sentry ON): `ro_usb`
>    clip → 206 via the SPA, archive clip 6 (60 MB) decodes+plays, 0 console errors,
>    recording never disturbed (0 USB events in the deploy window). See §4.2 +
>    `files/hw-results.md`.
>
> **Map page V1 parity** (operator "exact V1 mirror"): All-Clips per-clip action
> buttons (Play / Show-on-Map / Download / Archive[cloud-gated] / Delete) + ClipDto
> `lat/lon`; EventPlayer `ro_usb` playback. Same commit. (UAT trip-map 66/66.)
>
> **Deferred residuals (operator-aware):** (a) a substitute file identical in BOTH
> `valid_data_length` AND `data_length` still passes the size gate — needs a content
> fingerprint at ingest; (b) a >16 MB clip can truncate mid-stream after headers
> (errors the body, never serves wrong bytes as complete).
>
> **SEI/GPS coverage requirement — VERIFIED 2026-06-25 (no code change).** Operator
> rule: "all videos except stationary non-event sentry should have SEI/GPS data."
> Audited the full pipeline (Opus read + explore-agent map + a gpt-5.5 adversarial
> disconfirm review, all reconciled): `scannerd produce.rs::shape_front` runs the SEI
> walk for **every front clip across all folder classes** (no class gate, only the
> stability gate); per-sample `has_gps_fix` (`lat≠0||lon≠0`) persists to indexd
> `clip_waypoints` unconditionally; `webd waypoints_for_clips` exposes `ClipDto.lat/lon`
> = first `has_gps_fix=1` waypoint. So a moving/event clip always surfaces its GPS, and
> only stationary clips with no fix lack a pin. The live device's 0 GPS fixes = parked
> Sentry footage (compliant). gpt-5.5 raised 9 items; reconciled: #1 decimation
> (1/s, v1-parity), #2 256 MiB read cap (4–8× any real 1-min clip), #3 degraded-parse,
> #4 front-only SEI, #8 (0,0)-as-no-fix → all **parity-faithful / not reachable by real
> footage**; #7 GPS-bearing SentryClip not materialized as trip/event → **verbatim v1
> port** (`derive.rs:469`, `materializer.rs::materialize_sentry_clip`) — data still
> persists in `clip_waypoints`/`ClipDto`; #9 HUD parses the *selected* camera stream
> → **v1-parity-faithful** (v1 `switchCamera`→`loadVideoWithCache`→`loadSEIData(selectedUrl)`
> does the same; "always-front" would DIVERGE). Two genuine V1-parity follow-ups FILED
> (separate lanes, not this requirement): see below.
>
> **FILED V1-parity follow-ups (from the SEI/GPS doubt review):**
> - [x] **event.json as event-pin location source** — v1 `event_player`/mapping use the
>   Tesla `event.json` (`reason`, `city`, est `lat/lon`, `timestamp`) for event metadata;
>   B-1 derived events from SEI waypoints only, so a no-GPS Sentry **event** got
>   `lat/lon = None` (`derive.rs::materialize_sentry_clip`) and no map pin where v1 shows
>   one. **DONE 2026-06-29 (`fu-eventjson-pin`, commit `5046a46`):** scannerd parses
>   `event.json` (`clip_event.rs`) → indexd persists a `clip_events` sidecar (schema v3,
>   additive) and derives an estimated pin onto trip-less events → webd
>   `GET /api/events?day=YYYY-MM-DD` + `/api/days` union surface them → SPA TripMap renders
>   them as day-grouped standalone pins (clipless parked pins show description, no Watch
>   link). Tests: podman webd 327 / indexd+scannerd green, clippy clean; full Playwright
>   UAT green (both viewports) incl. a new mock-driven parked-day clipless-pin render test.
>   **Live-verified on `cybertruckusb.local`:** v3 migration applied cleanly (counts
>   preserved 176/5/6), and a real parked `saved` event (id 6, trip_id/clip_id NULL,
>   42.9215/-83.6211, "…Dashcam Launcher…Holly") surfaces via `/api/events?day=2026-06-29`
>   and renders as a single standalone map pin at both 1280px and 375px, 0 console errors.
>   Evidence: `files/hw-results.md`, `eventjson-live-desktop.png`, `eventjson-live-mobile.png`.
> - [x] **HUD client parser timing fallback parity** — B-1 `spa/src/player/dashcam-mp4.ts`
>   required the full `moov/trak/mdia/minf/stbl/stts` chain and returned `[]` on any miss;
>   v1 `dashcam-mp4.js` falls back to 33 ms/frame (`config.durations[i] || 33`).
>   **DONE 2026-06-29 (`fu-hud-stts-fallback`):** `frameDurations()` failure is now
>   non-fatal (caught → empty durations), so the existing per-frame fallback supplies
>   timing; fallback constant matched to v1's literal `FRAME_FALLBACK_MS = 33`. SMPTE-
>   fixture / normal-stts paths unchanged. Verified by a crafted-MP4 (mdat + Tesla SEI,
>   no moov/stts) smoke test: pre-fix 0 samples → post-fix 2 samples at t=0 / t=0.033s.
>   `tsc --noEmit` + vite build clean. (Not on-device-exercisable: parked Sentry footage
>   has no SEI.)
>
> **NEXT (operator-queued):** systematic V1-parity audit across all parity screens
> (exact-mirror UX). **Phase-2 (the deleter) stays deferred + GATED — do NOT start
> autonomously.**
> Parity-audit progress: **Slice 2 (indexer-liveness health dot)**, **A5 (Music
> rename-during-move)**, **A6 (Chimes "Edit"/re-trim)** + **A7 (EventPlayer
> download progress)** shipped & live-verified 2026-06-26.
> Next bucket-A SPA item: **A8 (LightShows mobile checkbox overlap — needs design
> decision first)** — verify each against V1 source before building (see
> `files/v1-parity-audit.md`).
>
> Build/test via podman from PowerShell (see copilot-instructions.md) — never local WSL/cargo.
>
> ---
> _Earlier resume snapshots below are historical._

> ## (history 2026-06-22) — Phase-1 archive daemons live; `p1-playwright` SPA wiring DONE+proven; live DECODE gated on real footage (C3)
>
> **Phase-1 (archive-only, non-destructive) is CODE-COMPLETE + host-green; on-device
> verification is what remains.** The operator-approved two-phase plan split the
> recording-critical deleter (Phase-2, deferred + gated) from a safe archive-only first
> slice (Phase-1) that copies completed RecentClips off the car partition into the
> Pi-side archive and registers `view_kind='archive'` with indexd so webd can stream them.
> This unblocks §4.2 #2 (live-clip map playback) without deleting anything.
>
> **⚠️ Phase-1b correction (ADR-0004).** The first Phase-1 retentiond read the car
> partition by **mounting `teslacam.img`**, violating ADR-0003's #1 invariant (the Pi
> must NEVER mount the live car image). Caught during hardware prep, reconciled with
> GPT-5.5, and re-implemented mount-free. The source-read layer now: inventory ← a
> **read-only `indexd` SQLite** candidate query (webd's connect pattern); bytes ← a new
> **`scannerd` `ReadFile` socket** (`/run/teslausb/scannerd-read.sock`) that serves raw
> exFAT windows via `pread` with targeted descent + identity fence, never mounting.
> See [`adr/0004`](./adr/0004-retentiond-archive-read-path.md) +
> [`contracts/scannerd-readfile.md`](./specs/contracts/scannerd-readfile.md).
>
> **Phase-1b lanes — DONE, committed (NOT pushed), each GPT-5.5-reviewed:**
> - `049605d feat(scannerd)` — `ReadFile` socket: targeted component descent (no full
>   walk), per-component torn-entry→NotFound, `valid_data_length` clamp, bounded
>   window read, identity fence + post-read re-resolve. Podman: 90 tests, clippy clean.
> - `a015941 refactor(retentiond)` — mount-free archive driver: read-only indexd SQLite
>   candidates + streaming `ReadFile` copy (never buffers a whole clip) + temp/fsync/
>   atomic-rename + orphan-angle rollback on abort. Podman: 122 tests, clippy clean.
> - `f1b6d3d docs(adr)` — ADR-0004 + spec alignment. Wire fixtures match byte-for-byte
>   across both crates.
> - (Earlier mount-era lanes `20884b2`/`257675a`/`6fe9d33`/`6808c76` superseded by the above.)
>
> **`p1-hw-deploy` — DONE (2026-06-21, PASS).** Cross-built aarch64 (indexd + scannerd +
> retentiond) via podman; deployed under the dead-man rails (hardware-test skill) after a
> GPT-5.5 deploy-plan review (BLOCK → all 8 fixes applied). No synthetic footage / no
> `teslacam.img` RW-mount was needed — the live catalog already held one RecentClips
> candidate (clip id 5), and the archive path is strictly read-only on `teslacam.img`.
> retentiond archived clip 5's 4 angles (`ro_usb`→`archive`, 49192 B each) into
> `/data/teslausb/archive` and registered them with indexd; webd `/api/clips/5/stream`
> returns **206** for all 4 cameras (was 404). teslacam.img size+mtime unchanged, no new
> failed units, retentiond now `enabled`. Evidence: session `files/hw-results.md`
> (+ `hw-p1deploy-*.log`). Deploy invariant held: retentiond `--archive-root` ==
> `WEBD_ARCHIVE_ROOT` = `/data/teslausb/archive`. **NB (found 2026-06-22): 206 proves
> the streaming wiring, NOT decodability — clip 5 and every clip on the device are
> synthetic ~48 KiB stubs (`ftyp`+`mdat`, no `moov`); see `p1-playwright` below.**
>
> **NEXT (Phase-1 verification):**
> 1. `p1-playwright` — **SPA portion DONE + proven live 2026-06-22.** All Clips list →
>    clickable clip row (`/events?clip=<id>`) → EventPlayer clip-by-id resolve →
>    `#mainVideo` src `/api/clips/<id>/stream` → **HTTP 206 with full bytes on all 4
>    cameras**, TTFB ~14 ms, FCP ~280 ms, console clean (`files/hw-results.md`).
>    **§4.2 #2 stays UNCHECKED: actual DECODE/playback is blocked** — the live
>    `teslacam.img` holds only synthetic ~48 KiB stub clips (no `moov`). Measured
>    read-only: every angle is exactly 49192 B with exFAT
>    `DataLength==ValidDataLength==49192`, so scannerd reads faithfully and there is
>    **no truncation bug** (an earlier in-flight hypothesis + a prior GPT-5.5 opinion,
>    both refuted by direct measurement). The decode proof is gated on real footage
>    (Tier-C **C3**). Two robustness follow-ups were filed under §4.2: the retentiond
>    `moov`/decodability gate — **DONE 2026-06-22** (quarantines undecodable archive
>    copies instead of publishing them; spec `contracts/indexd-archive-register.md` §9;
>    FU-1..FU-6 filed) — and the scannerd VDL-clamp check (still open, gated:C3).
>
> **⚠️ SELF-SUFFICIENT ARCHIVER re-architecture (ADR-0005) — DONE + HARDWARE-PROVEN 2026-06-29.**
> Triggered by a real data-loss incident: a return drive's footage was permanently lost
> because the old `retentiond` depended on `indexd` (work-list) **and** `scannerd` (clip
> bytes); both livelocked during the drive and retentiond restart-flapped, so nothing
> archived and the live RecentClips circular buffer recycled the clips. Operator directive:
> **the archiver must be independent of every other daemon and always operational**, and
> **I/O/CPU/memory efficient** (constrained Pi sharing one microSD with live recording).
> Re-implemented via two trait seams feeding the UNCHANGED archive pipeline: (1)
> `VolumeCandidateSource` reads `TeslaCam/RecentClips/*` directly from `teslacam.img` via
> scannerd's *library* (compile-time dep, **not** the daemon) — targeted root→TeslaCam→
> RecentClips descent, exFAT decode via `teslausb_core::dir_decode`, 2-scan/60s stability
> gate; (2) `VolumeReadFileClient` reads bytes volume-direct via `pread`. Dedup is
> **archive-local, content-addressed on-disk markers** (`.retentiond/markers/*.json`, keyed
> on canonical_key + cheap FAT-chain `source_fingerprint`, no clip-byte reads); indexd
> registration is **best-effort** via a durable outbox (`.retentiond/register-outbox.json`)
> and NEVER blocks archiving. Unit (`deploy/systemd/retentiond.service`) is now
> dependency-free: `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=0`,
> `IOSchedulingClass=idle`, `CPUWeight=10`/`IOWeight=10`, `OOMScoreAdjust=100`, `LimitCORE=0`,
> `RequiresMountsFor=/data/teslausb`. 162 tests pass, clippy clean, aarch64 cross-build green.
>
> **Hardware deploy (cybertruckusb.local) under the dead-man rails — PROVEN:**
> - First deploy canary FAILED: every cycle errored `invalid cluster 0: chain start out of
>   range` — my targeted walk had REIMPLEMENTED scannerd's traversal but OMITTED its
>   `is_valid_cluster(first_cluster)` guard (`walk.rs:143`); an **empty exFAT dir reports
>   `first_cluster=0`** and `follow_chain(0)` aborted the whole cycle. Rolled back safely.
> - Fixed: added the guard in `read_dir_entries` + `chain_digests_for_records` (+2 regression
>   tests). Re-deployed (binary `2b9f170a…`); canary clean (NRestarts=0, no cluster-0 error,
>   markers + outbox created, heartbeat via the Ok path).
> - **Decisive self-sufficiency test PASSED:** stopped **both `scannerd` and `indexd`**, and
>   retentiond kept cycling, reading the volume directly, **no dependency error, no crash**.
>   gadgetd recording untouched throughout; `df /data` held flat (no duplication, no
>   OS-starvation). Evidence: `files/hw-results.md`.
>
> **Known characteristic — cold-start migration (ONE-TIME, self-resolving):** the FIRST boot
> of the marker-based binary has zero markers (the old binary used indexd-based dedup), so
> its first cycle re-copies the current volume RecentClips buffer (~2 GB observed) to seed
> markers, contending with recording and freezing the 180s health heartbeat for the duration.
> This does **not** recur: markers persist on `/data` (survives reboot) → every later boot
> dedups cheaply with no byte reads. A `gpt-5.5` adversarial review **NO-GO'd** a
> size-match "adopt-existing-archive" fast-path (it could permanently bless wrong/corrupt/
> truncated bytes = silent clip loss); a *safe* adopt would have to re-hash+re-probe the dest
> anyway, mostly defeating the savings — so it was abandoned.
>
> **Copies-per-cycle cap + per-clip heartbeat — IMPLEMENTED + DEPLOYED 2026-06-29 (evening).**
> `archive_recent_once` refactored into `archive_recent_capped(.., max_copies: Option<usize>,
> on_progress: &mut dyn FnMut())`; the old fn is now a thin `None`/no-op wrapper so all ~35
> existing call sites/tests are unchanged. Serve loop calls it with `Some(4)`. The cap counts
> only candidates that enter the copy phase (cheap dedup/already-pending skips do NOT consume
> budget and do NOT fire the callback); candidates are processed **oldest-first** (= eviction
> order, verified `volume_source.rs:435`), so the most-at-risk clip is always archived next.
> After the cap a cycle returns → serve loop writes a fresh heartbeat and sleeps 20s (idle gap
> = gentler on recording), then resumes next cycle (already-copied clips dedup-skip cheaply).
> `on_progress` keeps the heartbeat live mid-batch. A `gpt-5.4-mini` adversarial review found
> ONE real issue — the end-of-cycle heartbeat reused the loop-entry timestamp, clobbering the
> fresher `on_progress` writes; **fixed** by stamping a fresh `cycle_end` time. 166 tests pass,
> clippy clean, aarch64 cross-build green (binary `a52017f2…`). Hardware canary clean:
> NRestarts=0, 0 cluster-0 errors, heartbeat `updated_at` confirmed advancing (+64s across
> reads, no false-stale), pending=0, wifi up, `df /data` 220 G free. Dead-man cancelled; device
> healthy overnight. The self-sufficient core was committed as `872e5e2`; the cap+heartbeat diff
> was committed as `d1d73bb` (source of the deployed `a52017f2` binary; tested + reviewed + proven)
> and pushed to `origin/mhackermsft/b1-clean`.
>
> **MARKER JOURNAL: in-memory index + bounded prune + non-destructive copy (ADR-0006) —
> IMPLEMENTED + REVIEWED + GATED 2026-06-30.** Operator approved the full-scope option after a
> GPT-5.5 second opinion rejected an earlier "prune is inherently safety-neutral" framing. Three
> parts, all in `rust/crates/retentiond/`:
> **(A) In-memory marker index.** `DriverState` gains `markers: HashMap<canonical_key,
> MarkerSummary{source_fingerprint,status,last_seen_epoch,missed_scans}>` loaded ONCE at startup
> (mirrors `load_outbox_if_needed`), skipping parse/schema failures AND off-path markers whose
> file name ≠ `stable_hex(canonical_key).json`. `marker_is_complete_live` now reads the MAP
> (zero per-cycle marker file reads). `write_marker` writes the durable file FIRST, upserts the
> map only on success → the cache can never claim CompleteLive without a durable marker.
> **(B) Conservative source-aware prune.** Bounds marker growth to ≈live-window size. A marker
> is pruned (file + map) only after `missed_scans≥40` consecutive absent scans AND `≥3600s` wall
> floor; runs every 5 cycles, ≤16 deletions/cycle; refresh-before-prune so a live clip is never
> pruned; only the deployed `VolumeCandidateSource` enables it (the SQLite source filters
> archived clips and must NOT). On a real (non-NotFound) delete failure the index entry is KEPT
> so the map never diverges from disk.
> **(C) Non-destructive staged-promote copy.** Fixes a PRE-EXISTING latent loss bug GPT-5.5
> found: the old rollback deleted already-archived angles on a mid-clip re-copy failure. Now each
> angle stages to `.retentiond/staging/…`, hashed; only after ALL stage OK are they promoted
> (rename) to final via new `ArchiveStore::promote_dest`. Any failure discards STAGING only —
> finals are never touched. Startup wipes `.retentiond/staging/` (crash orphans).
> **Process:** GPT-5.5 design second-opinion + adversarial code review (verdict: no blocking
> footage-loss/index-divergence; 3 hardening items applied + tested, 1 fsync item consciously
> skipped as footage-safe + I/O-costly). **178 tests pass, clippy clean, aarch64 cross-build
> green (binary `868a630e…`).** **DEPLOYED + LIVE-VERIFIED 2026-06-30** under the dead-man rails
> (commit `fde2e09`, pushed): new binary running (NRestarts=0), cycles clean (observed≈178,
> registered=4/cycle, copy_failed=0, pending=0, no errors), heartbeat advancing, staged-promote
> copy proven non-destructive (staging created→promoted→cleaned each cycle, zero byte loss),
> prune correctly conservative (markers 204→209, won't fire until the 40-scan/1hr threshold),
> wifi/SSH/boot untouched. Evidence: `files/hw-results.md` §Marker journal. ADR at
> `docs/adr/0006-retentiond-marker-index-prune.md`.
>
> **SYSTEMD SERVICE WATCHDOG (ADR-0007) — DEPLOYED + LIVE-VERIFIED 2026-06-30.**
> Completes the "archiver always operational" mandate: `Restart=always` only
> recovers a crashed archiver, not a hung one. retentiond now sends
> `sd_notify("WATCHDOG=1")` (hand-rolled over `UnixDatagram`, zero new deps,
> rate-limited ≤10s) from a process-global best-effort `watchdog` module; the
> unit gains `WatchdogSec=240` + `NotifyAccess=main` (kept Type=simple,
> Restart=always). Pets sit in the serve loop (on_progress, Ok/Err arms, 1s
> sleep tick) AND inside the read/hash/copy chunk loops, plus boundary pets
> (sync_all/rename, startup staging wipe + marker load, candidate scan). A false
> kill is bounded-no-loss (non-destructive staged-promote + durable markers,
> ADR-0006), so a hung cycle is now restarted within ~240s with zero footage
> loss. **Process:** GPT-5.5 design 2nd-opinion (WatchdogSec, per-chunk gap) +
> 2 adversarial code reviews (false-kill findings reconciled; bounded stop
> documented) + a GPT-5.5 deploy-plan review (NO-GO-as-written → binary-first
> ordering, 180→240s, strengthened canary, /home/pi staging — all adopted).
> **184 tests, clippy clean, aarch64 `98915f74…`.** Deployed under the dead-man
> rails (commits `d76ebef` + `33fb7b1`, pushed): watchdog ARMED (WatchdogUSec=240)
> + continuously pet (WatchdogTimestampMonotonic advancing), new binary running
> (`/proc/$MainPID/exe`=98915f74), NRestarts=0 over the window (no false kills),
> health advancing, StartLimitIntervalUSec=0, wifi/SSH/boot untouched (degraded =
> pre-existing unrelated zram timer only). Idle-path load-scope caveat recorded.
> Evidence: `files/hw-results.md` §retentiond systemd service watchdog. ADR at
> `docs/adr/0007-retentiond-systemd-watchdog.md`.
>
> **Phase-2 (the deleter) stays deferred + GATED** — leases/recovery, indexd delete-RPC,
> gadgetd delete-handoff client, C2 governor calibration, safety gates + explicit operator
> opt-in. Do NOT start autonomously.
>
> Build/test via podman from PowerShell (see copilot-instructions.md) — never local WSL/cargo.


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

## ⏯️ Resume here — A3e.4 (no-RTC clock-plausibility gate) DONE + live-proven; pick next item (2026-06-18)

**Branch `mhackermsft/b1-clean`.** A3e (device timezone / DST-correct enforcement) and its
follow-up **A3e.4 (no-RTC clock-plausibility gate) are both DONE and live on hardware**:
- **A3e.4 deployed + live-proven 2026-06-18** (cross-built aarch64 webd+schedulerd via podman,
  atomic-mv under dead-man rails). With `WEBD_CLOCK_PLAUSIBLE=0` the device logged the boot +
  tick skip lines (once each), the active LockChime stayed unchanged (time-windowed schedule
  skipped, random-on-boot preserved), and normal enforcement resumed cleanly on removal; no
  reboot, no failed units, SSH/WiFi up. Coded by GPT-5.3-codex to an Opus spec; GPT-5.5
  adversarial code + deploy-plan reviews reconciled. Tests: core 343 / schedulerd 32 / webd 265
  (5 new). Evidence: `files/hw-results.md` "A3e.4" + `files/hw-a3e4-clockgate-journal-*.log`.
  See full §4.5 A3e.4 entry below.
- **Coder switch (2026-06-18):** retired `mai-code-1-flash-internal` for coding (unscoped
  workspace-wide `cargo fmt` + unreliable self-reported tests); coder is now **GPT-5.3-codex**
  (well-defined coding tasks only — Opus owns all planning/reasoning/review-reconciliation).
  Recorded in `.github/copilot-instructions.md`.
- **Review-speed policy (2026-06-18):** risk-tiered reviews — trivial→Opus-only, small+green→one
  scoped medium-effort review run in parallel with tests, high-risk/live-hardware→full adversarial
  + cross-model. Reviews scoped to changed hunks (not full-file re-derivation), run parallel to
  podman verification.

**NEXT STEPS when resuming:**
1. Commit `docs/status.md` + `.github/copilot-instructions.md` + the 3 A3e.4 rust files + `files/`
   evidence (this update). NOT pushed unless asked.
2. Pick the next unchecked `status.md` item per the normal loop (partition non-colliding items
   into parallel lanes for throughput).

**Prior A3e timezone work** (DONE, live): commits `fff2ba9` + `6d64913`; deployed under hw rails
(removed DST-defeating `WEBD_TZ_OFFSET_SECS=-14400` drop-in, system tz America/Detroit,
`date +%z=-0400`). Evidence: `files/hw-results.md` "A3e.3" + `files/tz-deploy/`.

---

## (history) REALTIME MEDIA UPDATES LIVE (webd SSE push, replaces list polling) (2026-06-17)

**Shipped + live on hardware.** Media lists across all six screens (Music,
Boombox, Light Shows, Wraps, License Plates, Lock Chimes library) now refresh the
instant a change lands, instead of waiting on per-screen polling timers (2 s
first-poll delay) or a manual refresh.

- **Backend (`webd`, no `gadgetd` touch):** new `media_events.rs` runs a background
  monitor over a dedicated read-only catalog connection watching SQLite
  `PRAGMA data_version`; it ticks a `tokio::broadcast` whenever `indexd` commits.
  New `GET /api/media-events` SSE forwards each tick as a `media-changed` event.
  Monitor thread holds a `Weak` and self-terminates when `AppState` drops
  (test-safe). Wired in `lib.rs` (`AppState.media_events`, started before catalog
  is moved) + `route.rs` (`media_events_stream`).
- **Frontend:** singleton `EventSource` client `api/mediaEvents.ts` (lazy-open,
  ref-counted close, 150 ms debounce, native auto-reconnect). `useMediaCategory`,
  `useMusicLibrary`, and `ChimeScheduler` each subscribe and silently refetch on a
  tick — fixing the `useMediaCategory` single-shot-fetch staleness bug outright.
  Per-op "syncing"/"removing" convergence badges unchanged.
- **Verified:** `cargo test -p webd` 256 pass (incl. 2 new `media_events` tests
  proving tick-on-external-commit + thread shutdown). Playwright UAT full suite
  green (read-only specs updated to allow the new `GET /api/media-events`).
- **Live (2026-06-16, hw rails, webd-only restart):** deployed webd aarch64
  `926eaaa3` + realtime SPA to `cybertruckusb.local`; uploaded a throwaway file →
  `event: media-changed` pushed over the SSE 25 s later (the indexd reindex
  moment), with the file present in `/api/music` at that instant. gadgetd never
  touched (uptime continuous, all services active, WiFi/boot intact). Evidence:
  `files/hw-results.md`.



**Stopped for the night 2026-06-15 ~21:25.** Branch `mhackermsft/b1-clean`,
working tree **clean** (all work committed, **nothing pushed** — push tomorrow if
desired). Device `cybertruckusb.local` healthy: SSH + wlan0 connected, boot
`degraded` (pre-existing benign zram timer), webd serving the latest SPA bundle.

**Shipped + LIVE-PROVEN today (newest first):**
- **Lock-chime client-side audio editor (V1 parity)** (2026-06-18, commit `22bd73e`):
  single-file chime upload now opens an in-browser editor matching the V1 UX — trim,
  optional LUFS normalization (Broadcast/Streaming/Loud/Maximum presets), and re-encode
  to 16-bit PCM WAV — while 2+ files keep the batch path. All processing is client-side
  via the Web Audio API; the webd contract (16-bit PCM WAV, mono/stereo, 44.1/48 kHz,
  ≤1 MiB) is unchanged. Output is frame-clamped to `maxFrames` and double size-asserted
  ≤1 MiB before POST; the fallback (Web Audio unavailable) only uploads an already-valid
  WAV and refuses oversize sources; retryable (503/network) failures render retryable not
  fatal; decode races are job-token guarded. New `spa/src/audio/{wav-core,chime-engine}.ts`
  + `ChimeAudioEditor.tsx`. GPT-5.5 reviewed (1 blocker + 2 should-fix found and fixed,
  re-review clean). Build clean; 120 Playwright tests pass. Live on `cybertruckusb.local`
  under hardware rails (additive sha256-verified asset swap, atomic index flip, webd
  restart): device gate **20/20** both viewports, console clean, warm screen-ready ~300 ms.
  Evidence: `files/hw-results.md`, `files/chime-editor-device-{desktop-1280,mobile-375}.png`.
- **Boombox page accuracy: size text + folder wording** (2026-06-18): the Boombox
  requirements card and upload zone still advertised a **1 MB** per-file limit even
  though `BOOMBOX_MAX_BYTES` was raised to **8 MiB** on 06-17 — the page text was
  never updated. Corrected the card + zone (1 MB → 8 MB, both MP3 and WAV; the
  WAV validator caps format only, not size), clarified the folder line to
  `Boombox/` at the media-drive root (storage location was already correct — no
  backend change), and surfaced the enforced WAV spec (16-bit PCM, 44.1/48 kHz,
  mono/stereo). `docs/Requirements.md` §4.7 stale "≤ 1 MB" → "≤ 8 MB" with the
  LockChime-conflation rationale. Build clean; `boombox.spec.ts` **24/24** both
  viewports. Commit `00d7200`. DEPLOYED to `cybertruckusb.local` (SPA-only,
  `index-gZ53aHov.js`, sha256-verified additive swap, webd active); device
  Playwright gate **14/14** both viewports, console clean.
- **Light-show + boombox upload size raised + clean over-limit errors** (2026-06-17):
  light shows ship with full-song audio (`.mp3`/`.wav`) that routinely exceeds the
  old **5 MiB** cap, so legit files (7–13 MB) were rejected. Raised
  `LIGHTSHOW_MAX_BYTES` 5 → **32 MiB** (body limit 16 → 34 MiB) and, same fix,
  `BOOMBOX_MAX_BYTES` 1 → **8 MiB** (body limit 8 → 10 MiB) — `media.img` is ~1 GiB
  so these per-file caps stay safe. Also fixed the confusing
  **"Couldn't reach the device"** error: `read_file_upload` returned its 422 early
  *without draining* the multipart body, which reset the connection mid-upload on
  large files and the browser misread it as a network failure — it now drains the
  field before responding (cheap, bounded by the body limit). SPA classifier maps
  413 / `file_too_large` to a human "That file is too large…" message instead of the
  raw byte count. webd: 256 unit tests pass (boombox oversize test updated to the new
  cap) + aarch64 cross-build (podman); SPA: 22 light-show UAT pass incl. a new
  oversize-413 test. DEPLOYED to `cybertruckusb.local` — webd binary swapped (backups
  `webd.b1-backup-*`), SPA served `index-XGAKgEOJ.js`, webd + gadgetd active,
  `/api/lightshows` + `/api/boombox` 200.
- **Multi-file upload + live status — media screens** (2026-06-17): Boombox, Light
  Shows, Wraps, and License Plates upload zones now accept **multiple files at once**
  (drag-drop or picker), show **realtime per-file status** (pending • / uploading ↻ /
  done ✓ / error ✗) with an "Uploading n/m…" counter, and **auto-add the new files to
  the library list below with no page refresh** (refetch after each file lands, plus the
  shipped SSE catalog stream). Reworked the shared `useMediaCategory` hook to a
  multi-file model (`selectedFiles[]`, `uploadItems[]`, `uploadProgress`, client-side
  `accept` extension filtering + dedupe, sequential install with per-file status, Retry
  for failures) and extracted a shared `MediaUploadZone` component + `media-upload.css`
  so all four screens render an identical zone. SPA-only (no gadgetd). `npm run build`
  clean; full UAT suite = **362 passed** (desktop-1280 + mobile-375), incl. multi-file
  DnD + an upload-flow integration test proving the live-list refresh. DEPLOYED to
  `cybertruckusb.local` — served `index-B8uqcrMo.js`, webd + gadgetd active.
- **Drag-and-drop upload — media screens** (2026-06-17): Boombox, Light Shows, Wraps,
  and License Plates now accept dragged files onto their upload zone (previously only
  click-to-choose worked — the drop zones had no DnD handlers). Added a reusable
  `useFileDrop()` hook (manages the `.dragging` highlight + `preventDefault` on
  dragover/drop, reads `dataTransfer.files`) and an `onFilesDropped()` handler on the
  shared `useMediaCategory` hook that stages the first dropped file (these are
  single-file uploads with count caps, unlike Music's multi-file). Per-screen
  `.dropzone.dragging` highlight CSS added. SPA-only (no gadgetd). `npm run build`
  clean; `npx playwright test` for the 4 specs = **70 passed** (desktop-1280 +
  mobile-375), each incl. a new real-`DragEvent` drop test asserting the file stages.
  **Live:** deployed `index-j1JdIkyN.js` to `cybertruckusb.local` (file swap,
  webd/gadgetd untouched + active).
- **Drag-and-drop upload — chime upload** (2026-06-18): the "Upload New Chime" panel
  on the Lock Chimes screen now accepts a dragged `.wav` as well as the file picker
  (previously click-to-choose only). Reused the same `useFileDrop()` hook as the other
  media screens, wired into the chime upload's existing single-WAV path: picker + drop
  share one `selectChimeFile()` (same `validateChimeWav`), single-file (first dropped
  file), inert while uploading, and a drop clears any stale picker value. New
  `.chime-dropzone` dashed zone + `.dragging` highlight. SPA-only (no daemon change).
  `npm run build` clean; media UAT = **42 passed** (incl. a new real-`DragEvent`
  drop→stage→upload test) + chime-scheduler UAT **56 passed**, both viewports, console
  clean. Also unblocked the media read-only allowlist for the already-shipped
  `GET /api/system/timezone` load fetch. **Live:** deployed `index-BUoGmtG_.js` to
  `cybertruckusb.local` (additive asset swap, sha256-verified, webd restarted; device
  Playwright gate 14/14 both viewports, dead-man cancelled clean). Commit `c6fd682`.
- **Multi-file upload — chime upload** (2026-06-18): generalized the "Upload New Chime"
  panel from one file to N. The file picker gained `multiple` and the `.chime-dropzone`
  now accepts multiple dropped `.wav` files (parity with the Music/Boombox multi-file
  flow). Each file is staged with a stable id and async-validated (tri-state:
  validating/valid/error); submit is disabled while any row is still validating so a
  file is never uploaded before validation resolves. Valid files upload sequentially
  with per-file status (pending/uploading/done/error) + a progress counter; on partial
  failure only the failed rows stay staged for retry, and any unattempted row (still
  validating or invalid) is preserved rather than silently dropped. The single-file
  success path is byte-identical (notice text + last-file `pendingUpload` convergence),
  so chime-scheduler UAT stays untouched. SPA-only (no daemon change). `npm run build`
  clean; media UAT = **46 passed** (incl. new multi-file drop + unattempted-preservation
  tests) + chime-scheduler UAT **56 passed**, both viewports, console clean. Reviewed by
  GPT-5.5 (one Important finding — upload-before-validated — fixed + regression-tested).
  **Live:** deployed `index-BkOitv39.js` + `index-BjyEKgIf.css` to `cybertruckusb.local`
  (additive asset swap, sha256-verified, webd restarted; non-destructive device gate
  10/10 both viewports, dead-man cancelled clean). Commit `b3660d2`.
  License Plates now fill the browser width like Music — their `.container` cards were
  capped at the global 1200px `.main-content` width, crunching the tables/galleries.
  Added a reusable `useFullWidthScreen()` hook (ref-counted body-class toggle) +
  `styles/fullwidth.css` (`body.screen-fullwidth .main-content { max-width: none }`),
  scoped so non-media screens keep the centred column. SPA-only (no gadgetd). `npm run
  build` clean; `npx playwright test` for all 5 specs = **104 passed** (desktop-1280 +
  mobile-375), each incl. a new full-width assertion (`screen-fullwidth` class +
  computed `.main-content` max-width `none`); desktop screenshot visually confirms the
  card spans edge-to-edge. **Live:** deployed `index-HxBWirkg.css` to
  `cybertruckusb.local` (file swap, webd/gadgetd untouched + active).
- **Active-card SOURCE name** (commit `2408e72`): the Active Lock Chime card now
  shows the *selected library chime's* name (e.g. `MarioFart.wav`) with an
  "Installed as LockChime.wav" subtitle, instead of the meaningless always-on
  `LockChime.wav`. Resolved client-side: just-activated name (while size matches)
  → unique library size-match → honest `LockChime.wav` fallback. The child
  `ChimeScheduler` reports its library up via `onLibraryLoaded`. 3 new Playwright
  tests (post-activate naming, cold-load unique size-match, size-collision
  fallback) — **72/72 green**. GPT-5.5 review: GO. **Live:** cold-load resolved
  `tesla-tron-lock-sound.wav`; in-session activate updated to `MarioFart.wav` with
  no reload, 0 console errors. See `files/hw-results.md` "Active card shows source
  chime name" + `files/hw-sourcename-1280.png`.
- **Set-Active UI auto-refresh** (commit `3e525b5`): active card + audio player
  update on their own after Set Active, no manual reload (details in the block
  below).

**Device live-test side effect:** the active lock chime on the car is now
`MarioFart.wav` (219770 B), changed during today's verification. Pick the
preferred chime via the (now-fixed) UI — it updates immediately.

**Recommended next steps for tomorrow (not yet started, all flagged below as
future):**
1. **Push** today's two commits (`3e525b5`, `2408e72`) if a PR/remote backup is
   wanted (operator decision — nothing pushed yet).
2. Continue working `status.md` items in the §4.5 chime area or pick the next
   unchecked foundation/feature item per the build order. Candidates already
   noted as future: **chime rename** (v1 rename API), **mp3→WAV transcode +
   multi-file + 5 s/normalize on upload**, **event-driven scannerd rescan** (to
   shorten the ~15–25 s activation convergence), **groups CRUD round-trip
   verify**, and the car-side pickup **C1** check (car-only).
3. Re-run the live UI smoke (`/media`) after any further SPA change — Playwright
   on `cybertruckusb.local` is the binding gate for UI work.

---

## ⏯️ Resume here — SET-ACTIVE UI AUTO-REFRESH LIVE (active card + audio, no reload) (2026-06-16)

**Operator requirement (locked, now satisfied):** after clicking **Set Active**
on a library chime, the **Active Lock Chime** card *and* its audio player must
update on their own with a clear time expectation — no manual page reload. Three
reported symptoms ("sometimes it doesn't seem to happen", a vague "syncing"
notice, and the player only playing the new sound after a reload) all had one
root cause: Set Active refreshed nothing on the read side.

✅ **Shipped + proven live on `cybertruckusb.local` (see `files/hw-results.md`
"Set Active auto-refresh" + screenshots
`files/hw-setactive-{desktop-1280,mobile-375}.png`):**
- **Root cause:** the active card + `<audio>` live in the parent `Media`
  (`GET /api/chimes`, loaded once on mount); the **child** `ChimeScheduler` Set
  Active button had no channel to refresh the parent → stale until reload.
- **Fix (mirrors the upload auto-refresh):** child raises `onActivated(filename,
  bytes)` after the `202`; parent records `pendingActivation = {filename, bytes,
  token, preModified, preSize, phase}` and **bounded-polls** `GET /api/chimes`
  (2 s interval, 60 s cap) until the active file reflects the activated chime,
  updating the card/audio on every poll (no reload).
- **Convergence key (robust):** `installed.size_bytes === activatedBytes` AND
  (nothing was active before, OR the size changed, OR the mtime became
  readable/advanced) — handles null mtime + same-second granularity + same-size
  re-activation, defeating the false-positive a mtime-only or size-only key has.
- **Audio reload:** `<audio key={installed.modified ?? size}>` remounts so the
  new bytes actually load (not a stale buffered chime).
- **UX:** in-flight "Applying … usually 15–30 seconds…"; on convergence "‘<name>’
  is now your active lock chime."; on 60 s timeout "Still applying …" + a
  **Refresh now** button. **All** Set Active buttons are disabled while a handoff
  is pending (syncing *and* waiting) so an in-flight activation can't be raced by
  a second one (prevents misattribution).
- **Verified:** build clean; **media + chime-scheduler Playwright UAT green**
  (5 new Set-Active tests: auto-refresh, all-buttons-disabled, timeout→Refresh
  now, same-size→mtime convergence, first-chime-with-none-installed). **Live:**
  activated `XPLockChime.wav` → "Applying…" + both buttons disabled → active card
  **215 KB/23:57 → 277 KB/00:49** and audio `v=` cache-bust advanced, success
  notice shown, **no manual reload**, buttons re-enabled, **0 console
  errors/warnings**, backend `LockChime.wav` = 283744 B confirmed, screenshots
  @375 + @1280.
- **Reviews:** two GPT-5.5 adversarial cycles reconciled — cycle 1 (root cause +
  weak convergence key), cycle 2 found 4 issues (status hidden when no chime
  installed; size-only convergence; missing poll try/catch; button re-enable in
  waiting) — **all 4 fixed**, the last by disabling buttons through both phases.
  **Deploy:** SPA-static-only (no Rust binary) under the hardware-test dead-man
  wrapper; snapshot `spa.b1-backup-20260615-204819`; wlan0 + SSH intact; webd
  serving new bundle `index-KZ1j0wlF.js`.

**Note:** the device's active chime is now `XPLockChime.wav` (set during this
live test). The operator can pick their preferred chime via the now-fixed UI.

---

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
6. **The Pi-side archive (and any unbounded growth: thumbnails, SQLite/WAL,
   upload staging, logs/journald) must NEVER starve the OS partition.** The card,
   `/`, and `/data` share one ext4 filesystem (`mmcblk0p2`), so archive growth can
   directly consume the bytes the OS needs to boot/run — left unchecked it can make
   the device **fail to boot or crash**. The storage governor MUST enforce a
   **hard, sacrosanct OS/app reserve** (`storage.md §2` tier-1, floor `max(5%, 2 GiB)`
   on shared-fs) that the archive can **never** consume: when free space approaches
   that reserve the governor evicts archive (oldest/lowest-value first) and denies
   new write budget *before* the OS is endangered — even if archive usage alone
   looks fine. This protection is not optional and is independent of the LUN sizes.

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
- [ ] Configurable advertised capacity per drive, fully pre-allocated.
  **Setup defaults (operator decision 2026-06-29): TeslaCam `lun.0` = 128 GiB
  (`--teslacam-mib 131072`, matches the factory Tesla USB-drive capacity), Media
  `lun.1` = 1 GiB (`--media-mib 1024`); the remainder of the card is left for the
  ext4 archive + OS/app files.** Defaults now applied in code (`gadgetd`
  `DEFAULT_TESLACAM_MIB`/`DEFAULT_MEDIA_MIB`) and the bootstrap unit
  (`deploy/systemd/gadgetd-provision.service` ExecStart). Provisioning is
  create-only-if-absent, so this changes **fresh installs / future provisioning
  only** — it does NOT resize an already-provisioned image. **Still TODO:**
  user-configurable at setup time (V1-style: recommend sizes from the card's total
  capacity, provision only if absent) surfacing `--teslacam-mib`/`--media-mib`;
  online resize lives in §4.11. **`exfatprogs` (`mkfs.exfat`) must be installed on
  the device** or `gadgetd provision` fails at the mkfs step.
  **LIVE-DEVICE ROOT CAUSE (2026-06-28 incident — "TeslaCam drive full"):** the
  live `teslacam.img` was provisioned at the old 3 GiB placeholder default, far
  below what the car needs (~64 GB min / 128 GB factory) → the car's exFAT
  partition filled in ~minutes and stayed red-X; in-car deletes can't recover
  because the LUN itself is too small. **RESOLVED ON LIVE DEVICE 2026-06-29:** the
  live `teslacam.img` was re-provisioned 3 GiB → 128 GiB under hardware-test rails
  (GPT-5.5-reviewed plan: patch the boot provision unit first, `mv`-not-`rm`
  rollback, stop reader daemons, verify exFAT before rebind). New 128 GiB
  `TESLACAM` exFAT bound + `udc configured`; 294 GiB free after; 18 GiB Pi-side
  archive preserved (recent un-archived car clips on the old image were lost — an
  accepted one-time cost of the fix). See `files/hw-results.md` "Live re-provision".
  Car should re-enumerate and clear the "drive full" warning on next drive. **(see
  §4.11 resize) (gated:C1)**

### 1.1 Change propagation to the car

- [ ] **Soft SCSI medium-change** on `lun.1` after directory changes (new/deleted
  media) — car re-reads listings without re-plug; `lun.0` unaffected. **(port
  `tesla_cache_invalidate.sh` behavior into gadgetd)**
- [x] **Full USB re-enumeration** ONLY for an active-`LockChime.wav` change, with a
  bounded health check that recording resumes. **(port `tesla_gadget_rebind.sh`)**
  **(HW-PROVEN & operator-confirmed 2026-06-24 — set chime in SPA → gadgetd auto
  re-enumerates (`reason=chime_apply`) → car re-reads + plays the new chime on
  next lock; `lun.0`/recording untouched. See `files/hw-results.md` + spec
  [`gadgetd.md` §9](specs/gadgetd.md). Three slices: **i1** = the gated
  `reenum::reenumerate` primitive with the recording-idle health gate (HW-PROVEN,
  shipped `2ea8ddb`); **i2** = gadgetd auto-fires that primitive after a successful
  `InstallFile(LockChime.wav)` on P2 so the SPA "set chime" reaches the parked car
  with no manual IPC (committed `73e2414`). i2: durable sha256-token pending state
  persisted *before* the staged blob is reclaimed (never lost across power loss),
  separate 2s scheduler thread gated on recording-idle + all-queues-empty, exp
  backoff on failure, `gadget_status` exposes `chime_reenum_pending`+`last_reenum`.
  GPT-5.5 adversarial review GO (2 cycles); `cargo test -p gadgetd` = 148 pass;
  aarch64 cross-build verified. **i3** = the SPA shows a full-screen blocking
  "Syncing chime to your car — keep the doors closed" overlay while
  `chime_reenum_pending` is true, then updates the activation notice to
  "…on the next lock" when it clears (webd `map_gadget_status` passes through
  `chime_reenum_pending`+`last_reenum`; monotonic per-activation tokens close a
  stale-poll race that could mislabel/clobber the notice). GPT-5.5 adversarial
  review GO (3 cycles); full `media.spec.ts` = 54 pass at desktop-1280 + mobile-375,
  incl. 2 new race regression tests (each verified RED on the pre-fix code).
  **Recording (`lun.0`) never gated.** End-to-end
  car-pickup **HW-PROVEN 2026-06-24** (i2 auto-reenum; operator heard the new chime).)**
- [ ] **Hardware test:** confirm the car actually picks up directory changes via
  soft medium-change, and a chime change via re-enumeration (Requirements §1.1
  is a v1-observed behavior to re-verify on B-1). **(C)** **(chime-via-reenum half
  DONE — HW-PROVEN 2026-06-24: set chime in SPA → car re-reads + plays it after the
  i2 auto-reenum, recording intact (`files/hw-results.md`). Remaining: the **soft
  medium-change** half for directory listings (new/deleted media).)**

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
- [x] System-health status dot polling `/api/system/health` (green/amber/red/grey;
  click→Settings health). **(A-shell-1 UAT-proven: `Shell.tsx` polls on mount + every 30s
  via `api.systemHealth`, parses string `overall`, maps severity→dot-class + tooltip; hidden
  until first successful poll; on failure keeps last-known colour and NEVER hides — exact V1
  `base.html` parity, verified against v1 source. UAT `spa/test/uat/shell.spec.ts`
  (dot visibility/class/tooltip + theme persistence + clean console/network + perf, 375 + 1280).
  The global poll fires on every screen, so 10 read-only specs were taught to skip it via the
  shared `SHELL_POLL_ALLOWLIST` (boombox/license-plates/light-shows/music/wraps/media +
  analytics/event-player/failed-jobs/trip-map). Full UAT 455 pass, 0 Fix-A regressions.
  GPT-5.5 adversarial review ×2 → caught 4 specs the first pass missed + the V1 hide-on-failure
  divergence; reconciled. Subsystem-richness of the health payload still tracked under §4.12.
  COMMITTED + PUSHED `38d160c` (mhackermsft/b1-clean).)**
- [x] **USB/recording mode status dot (Fix B — semantics operator-approved).**
  V1 `base.html` shows a mode dot in the top bar: GREEN `status-present` when
  `present && bound && udc_state=="configured"`; stays green w/ tooltip change on
  `handoff_active`; GRAY `status-unknown` otherwise; NEVER blue `status-edit`. **(B-shell-2
  done: `Shell.tsx` adds a second 30s `api.gadgetStatus` poll driving a bare always-visible
  `#mode-dot` span inserted between the health dot and the theme toggle — mirrors the health
  dot's mounted/inFlight AbortController + superseded() out-of-order guard + no-op catch
  (keep-last-colour). Always visible (V1 server-rendered it on every page), starts gray, never
  blue. Tooltips: "USB drive connected to vehicle" / "USB drive busy — syncing" (handoff) /
  "USB status unknown" — faithful approximations; exact V1 Python `mode_label` strings are
  unrecoverable (v1 not in-repo) and are pending operator confirmation. Harness: the host's
  webd has no gadget socket (→503), so `/api/gadget/status` added to the shared
  `SHELL_POLL_ALLOWLIST` (one edit covers all 11 spec consumers incl. analytics' 2 loops) +
  a centralized default 200 route (`GADGET_STATUS_OK`) in the `probe` fixture (`helpers.ts`),
  overridable per-spec (Playwright LIFO) so media-hub's 503-degradation test still works.
  `shell.spec.ts` extended: present/syncing/gray-on-503 class+tooltip, DOM order
  (health→mode→theme), clean console/network + perf + screenshots at 375 + 1280.
  media-hub's 503 test loosened "exactly one 503 log" → ">=1" (the new global poll legitimately
  adds a second 503 when gadgetd is down; the non-503 `other` filter still catches real leaks).
  Tier-2 flow: gpt-5.3-codex implemented, gpt-5.4-mini review clean, build clean, 38 affected
  specs pass (shell + media-hub + analytics). COMMITTED `5fb4531` (mhackermsft/b1-clean).)**
- [x] **Operation-in-progress banner (V1 `base.html` parity).** When a USB
  file-operation/handoff is running, V1 renders a top banner under the header
  ("File operation in progress…" / "Completing soon…"). **(DONE — `Shell.tsx`
  derives `operationActive` from `handoff_active` in the existing 30s
  `api.gadgetStatus` poll (no new request); renders a `role=alert`/`aria-live=polite`
  banner after `</header>` with the exact V1 copy + a spinner. Error-path clears the
  flag (guarded by `superseded()`) so it can't stick on after a status blip.
  `style.css` `.operation-banner` reworked to design-system tokens (dropped the
  garish gradient + pulse/bounce per the anti-AI-aesthetic charter; removed a
  duplicate `@keyframes spin`); dark-theme spinner gets `border-top-color:currentColor`.
  UAT `spa/test/uat/shell.spec.ts` (active/inactive) 12 pass at 375+1280. LIVE-verified
  on hardware 2026-06-26: banner correctly **hidden** when no handoff active, both
  viewports, console + network clean. See `files/hw-results.md` "bb-folder+op-banner live".)**
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
- [x] Filters: **map bbox (pan/zoom)**, event type, severity, min distance — client-side
  over the loaded day. **(UAT-proven at 375px + 1280px: `spa/test/uat/trip-map.spec.ts`
  "filters — event type, severity, min distance, limit-to-view, restore defaults". bbox uses
  trip-bbox ∩ viewport intersection (self-validated against vertex-in-bounds); trip-linked
  events hide with their parent trip; default state reproduces the pre-filter render; design
  in `docs/specs/spa.md` §4 "Trip-map filters". GPT-5.5 reviewed → SHIP.)**
- [ ] Filters follow-up: multi-day **date range** picker (needs multi-day load +
  seed/UAT changes; deferred from the within-day filter lane). **(not started)**
- [x] Side-panel = independent **global catalog browser**, decoupled from the map
  filters. Per operator directive the **map** reacts to filters (day-scoped) while all
  three panel tabs (Events / Trips / All Clips) show the **whole catalog newest-first**
  with **progressive (infinite-scroll) loading** so the Pi never serves one giant list.
  **(webd keyset cursor pagination `(date DESC, id DESC)`, opaque base64url cursor with
  `MAX(id)` snapshot pin → no mid-scroll shift/dup, `400 invalid_cursor`; new
  `GET /api/trips/page`. SPA per-tab IntersectionObserver state machine with synchronous
  in-flight lock (no double-fire) + error→retry affordance that does NOT auto-retry-storm
  a failing device, incl. on the initial load. EventPlayer keeps its v1 chronological
  playlist via a client-side asc sort. Spec: `docs/specs/contracts/webd-api.md` §2.1.1 +
  `docs/specs/spa.md`. Verified: `cargo test -p webd` 280; `tsc` clean; full UAT 427
  passed (progressive-scroll cross-day-desc, paging-error no-storm, initial-load-error
  retry — desktop 1280 + mobile 375, console clean). GPT-5.5 reviewed → SHIP.)**
- [ ] Filters follow-up: optionally also let the panel be **filtered** to match the map
  (v1 filtered map markers only; superseded above by the global-catalog-browser directive —
  revisit only if per-tab filtering is later requested). **(not started)**
- [x] EventPlayer follow-up: `?event=` deep-links resolve beyond the newest 100 events
  (pre-existing 100-event fetch cap). **(SHIPPED — by-id lookup, not paginate-until-found.
  New `GET /api/events/:id` point lookup (`query.rs::get_event` + `route.rs::event_detail`,
  mirrors `trip_detail`); SPA `api.eventById`. EventPlayer gains a "direct-event" mode
  (separate `directEvent` state, symmetric with direct-clip): when `?event=<id>` falls
  outside the newest-100 window the event is fetched by id and played directly while the
  playlist `events` array stays pristine (nav hidden, no false fallback to the newest
  event). Stale-async guarded (per-effect AbortController + `resolveSeqRef` generation
  token); the rare out-of-window initial load awaits the by-id resolve BEFORE publishing
  the playlist so `currentEvent` never flashes `events[0]`/fires a wrong clip fetch (the
  common in-window path stays atomic). Genuine 404 / no-clip / abort all distinguished;
  `?clip=` fallback preserved on miss. Spec: `docs/specs/contracts/webd-api.md` §2.1 +
  `docs/specs/spa.md`. Verified: `cargo test -p webd` 287 (incl. `event_by_id_returns_the_event`,
  `event_by_id_missing_returns_404`); `tsc` clean; full `event-player` UAT 34 passed
  (desktop 1280 + mobile 375) incl. 3 new deep-link tests — out-of-window by-id resolve+206
  (asserts `/api/events/:id` used AND wrong playlist-top clip never fetched), missing-event
  notice fallback, `?event=bad&clip=N` clip fallback — plus the pre-existing gate-7 deep-link
  test; console clean. GPT-5.5 design 2nd-opinion + adversarial diff review → GO (2 SHOULD-FIX
  reconciled & fixed: non-atomic out-of-window load + tightened UAT proof).)**
- [x] Side panel tabs (Events / Trips / Clips) + source folder switch. **(proven;
  **V1-parity folder selector LIVE-verified on hardware 2026-06-26** — the Clips tab
  now mirrors V1 `mapping.html` `vpFolder` exactly: a 4-option folder selector
  (Recent / Saved / Sentry / Archived Clips) defaulting to **RecentClips**, one folder
  at a time, **no "All Clips" superset option**. (Operator decision: match V1 exactly
  rather than ship B-1's earlier whole-catalog "All Clips" view.) Every `/api/clips`
  request — including infinite-scroll paging and error-retry — carries
  `folder_class=<selected>`; switching folders aborts the in-flight fetch and refetches.
  `TripMap.tsx` `ClipsFolder` type drops `""`; UAT `spa/test/uat/trip-map.spec.ts`
  reworked to the per-folder model (synthetic 30-item RecentClips catalog preserves
  cursor-paging / no-retry-storm / end-reached coverage; retry tests assert
  `folder_class` on every request; filters test pins the folder stayed RecentClips via
  known Recent/Saved ids). Full trip-map+shell UAT 42 pass at 375+1280, console clean.
  GPT-5.5 adversarial review ×3 (caught the original "All Clips" parity miss + 2 test
  assertion-weakening SHOULD-FIX, all reconciled). Live proof: default RecentClips both
  viewports, exactly 4 V1 options in order, folder switch refetches with
  `folder_class=SavedClips`, zero console errors / non-2xx. See `files/hw-results.md`
  "bb-folder+op-banner live".)**
  - [x] **All-Clips per-clip action controls (V1 mirror)** — each clip row carries
    Play / Show-on-Map / Download / Archive (cloud-gated → hidden) / Delete,
    matching v1's `mapping.html` row controls; ClipDto gains nullable `lat/lon`
    (representative GPS fix; omitted for stationary non-event sentry per operator
    rule). Shipped 2026-06-25 (`2e0db7c`); UAT trip-map 66/66 (`spa/test/uat/
    trip-map.spec.ts` "map panel — clips action buttons mirror v1 row controls").
  - [x] **Inline map video-overlay player — Slice 1 (V1 mirror)** — clicking a clip
    row on the map now opens V1's draggable floating overlay player (6-camera
    switcher, HUD shell, prev/next, download, fullscreen, maximize, delete; cloud
    archive hidden), instead of navigating to the full-page EventPlayer. Esc=unmaximize-
    only; outside-click/✕ close; ro_usb HEAD-probe gating; no-playable empty state;
    shared delete classifier (`player/deleteClip.ts`); pure angle predicates extracted
    (`player/angles.ts`). gpt-5.5 design + code review reconciled (3 BLOCKING + 2 SHOULD-FIX).
    UAT 86/86 both viewports; **live-verified on device** (overlay opens both viewports,
    interactive ~1.6s, zero console errors / non-2xx — see `files/hw-results.md`).
    Slices 2 (marker-open) + 3 (live telemetry HUD) pending.
  - [x] **Inline map video-overlay player — Slice 2 (V1 marker-open)** — clicking a
    map event marker's "▶ Watch video" popup link opens the inline overlay (V1
    parity) instead of navigating to `/events?event=N`; prev/next walks that trip's
    route clip sequence (per-trip events ordered by (t,id), deduped by clipId, no
    wrap). Typed controller `onWatchEvent` callback (leak-safe popup binding,
    fallback href preserved); TripMap owns the async `api.clip` fetch with
    AbortController + watch-seq/day-seq stale guards (rapid-click/day-change safe);
    markercluster/filtering/disambig untouched; EventsTab + EventPlayer routes
    untouched. gpt-5.5 design second-opinion (GO) + code review (no blockers; one
    pre-existing pagination SHOULD-FIX deferred). UAT 90/90 both viewports;
    **live-verified on device** (marker→overlay both viewports, zero console
    errors / non-2xx — see `files/hw-results.md`). Slice 3 (live telemetry HUD) pending.
  - [x] **Inline map video-overlay player — Slice 3 (live telemetry HUD)** — the
    overlay player's on-video HUD (gear, speed, steering wheel, brake/throttle pedals,
    L/R blinkers, autopilot) is now live, driven by the **reused `HudController`
    (unchanged)** + `telemetry.ts` — an exact mirror of EventPlayer's HUD. MapVideoOverlay
    constructs the controller once on mount (querying `#olGear`/`olSpeedVal`/`olWheel`/
    `olBrake`/`olThrottle`/`olBlinkerL`/`olBlinkerR`/`olAP2` within the stage) and reloads
    telemetry on `streamUrl` change (camera switch / ro_usb resolve); overlay-only CSS vars
    aligned to the controller's contract (`--wheel-rotation`, `--pedal-fill`) + `.oh-blinker.active`/
    `.oh-ap.active` styles. **Parity correction:** speed is always **mph** (V1 overlay HUD
    is hardcoded mph; the mph/kph pref scopes only to the map legend) — no kph in the overlay.
    No change to `HudController`/`telemetry.ts`/`EventPlayer.tsx`. UAT 92 both viewports
    (incl. "slice 3 HUD telemetry parity"); **live-verified on device** — fixture-driven HUD
    renders identically both viewports (gear R, 70 mph, wheel -18deg, throttle 42%, blinkerL
    active, "Autosteer" active), `mph` static, zero console errors / non-2xx (see
    `files/hw-results.md`). **Inline map overlay player feature complete (Slices 1-3).**
- [x] Units & timezone preferences re-render speeds/times. **(server-persisted: speed unit (mph/kph) + display clock (local/UTC) re-render trip-map speeds & times and survive reload; optimistic write with per-key serialized PUT /api/settings→indexd and rollback to the last server-confirmed value. Playwright: `spa/test/uat/trip-map.spec.ts` "display preferences (server-persisted)" — 20/20 green desktop+mobile, console clean. Follow-up: full IANA-zone picker + cross-screen time propagation.)**

### 4.2 Event / Video Player — `Requirements.md` §4.2

- [x] Stream **archived** clip with HTTP range (seek). **(proven for archived clips;
  live clips are the separate item below)**
- [x] **Play a recorded clip on the map by archiving it first (B1 archive loop).**
  A clip that is only on the car USB (`ro_usb`) is copied Pi-side by `retentiond`
  and registered as a `VIEW_ARCHIVE` angle, then plays through the normal archive
  stream path; reachable from the map's **All Clips** list (`/events?clip=<id>`).
  **PROVEN LIVE END-TO-END ON REAL FOOTAGE 2026-06-23 (`c3` gate, device in-car,
  Sentry ON):** real RecentClips archived (9 LIVE `archive_items`, ~1.49 GB; e.g.
  clip 8 = 6 angles incl. `left_pillar`/`right_pillar`, front 52 MB), `delete_state=
  LIVE` (decodability/`moov` gate passed on real muxing) → `webd` `/api/clips/8/stream`
  HTTP **206** with correct `Content-Range` on front + both pillars → **Chromium
  decode**: front 2896×1876 / left_pillar 1448×938, `currentTime` 0→3.001 s,
  62/73 frames decoded, **0 dropped**, console clean. Required the C3 fix (indexd
  pillar-camera allowlist + retentiond timeout/taxonomy/tombstone). See
  `files/hw-results.md`. (Earlier SPA play entry point proven 2026-06-22
  `p1-playwright`; full DECODE was gated on real footage — now satisfied.)**
- [x] **retentiond: decodability gate before publishing an `archive` angle.** DONE
  2026-06-22. After a copy lands (before registration), `retentiond::probe` runs a
  memory-bounded container-completeness check (top-level `ftyp`+`mdat`+`moov` with a
  parseable `moov/trak/mdia/mdhd`, `timescale>0`; any top-level box whose extent
  overruns EOF or whose header is malformed ⇒ unplayable; `mdat` never read into
  memory). If any angle fails, the whole clip is **quarantined** via a distinct
  `RegisterQuarantinedArchive` verb → `indexd` writes `archive_items.delete_state=
  'QUARANTINED'`, `durable=0`, and does **not** promote angles (stay `ro_usb` →
  `webd` 404), so a non-decodable copy is never served as a playable `archive` angle.
  Bytes are kept (zero clip loss); `remove_dest` stays reserved for copy failures.
  Fail-closed (old `indexd` rejects the verb → defer pending, never poison-dropped;
  deploy `indexd` before `retentiond`). Re-archive loop prevented (candidate SELECT
  excludes non-`DELETED` `archive_items`; pending `canonical_key` dedupe holds during
  retry). Spec: `contracts/indexd-archive-register.md` §9. Verified (podman):
  retentiond 126+14, indexd 68, teslausb-core 343 tests pass + clippy `-D warnings`
  clean; key test `archive_driver::tests::probe_error_routes_to_quarantine_without_copy_failure_or_remove`.
  Reviews: GPT-5.5 `moov-design-review` (design) + `moov-review`/`moov-review2` (diff).
  Will quarantine the live synthetic clip-5 stubs when deployed (expected; not a
  regression). Follow-ups filed below. **(follow-up from `p1-playwright`)**
  - [ ] **FU-1** — remediate clips force-promoted to `archive` *before* this gate
    (live clip-5 stubs stay playable-but-broken until a narrow Guard-A exception +
    re-validation driver downgrades them; low urgency, superseded at C3).
  - [ ] **FU-2** — finalization-aware archiving + explicit `QUARANTINED→LIVE`
    un-quarantine (gate selection on `mp4probe.complete`; allow re-archival when
    source bytes change so a not-yet-finalized segment isn't permanently quarantined).
  - [ ] **FU-3** — per-angle partial archive (archive good angles, quarantine only bad).
  - [ ] **FU-4** — quarantined-byte accounting metric (never auto-delete).
  - [ ] **FU-5** — strict nested-box extent validation in the MP4 probe, applied
    consistently across `scannerd::mp4probe` + `retentiond::probe` (today both reuse
    `teslausb_core::sei::mp4::find_box*`, which clamps child extents; theoretical gap
    not reachable by the truncation failure mode the gate targets).
  - [ ] **FU-6** — slot-aware archive path (pre-existing). `archive_item_path_for_candidate`
    drops the `slot:` prefix, so two clips on different slots sharing a timestamp would
    collide on one archive path. Unreachable in the single-slot RecentClips topology and
    never loses source footage (car volume read-only), but the path scheme should include
    the slot. Predates this gate (affected the LIVE path equally).
- [ ] **scannerd: confirm the `min(ValidDataLength, DataLength)` read clamp cannot
  truncate real Tesla footage.** No clamp gap on the synthetic test image
  (`VDL==DataLength`), but if a real car ever leaves `ValidDataLength` stale-small with
  recoverable data the clamp would silently truncate — a latent zero-clip-loss risk.
  Verify against real footage. **(follow-up from `p1-playwright`; gated:C3)**
- [x] **Stream a not-yet-archived clip directly from USB** (the lun.0 `ReadFile`
  fallback — ADR-0003 / `contracts/scannerd-readfile.md`). **DONE 2026-06-25.**
  `webd media.rs` falls back to a slot-0 `ReadFile` loop for a non-archive
  (`ro_usb`/`live`) angle when no `archive` copy exists; archive is always
  preferred. Hardened with a **first-read stable-size gate** (`contracts/
  scannerd-readfile.md` §5): on the probe read BOTH `readable_size`
  (valid_data_length) AND `total_size` (data_length) must equal the catalog's
  ingested `angles.size_bytes` → else **410** `clip_changed`; a NULL/≤0 catalog
  size fails **closed** (404, never serve unverified bytes); the per-request
  identity fence still governs later chunks. For a stable clip
  vdl==dlen==size_bytes ⇒ zero false-positive on legit clips. Implemented
  (gpt-5.3-codex) + 2 gpt-5.5 adversarial cycles (both-dimensions + fail-closed
  adopted). Gates: podman `cargo test -p webd` **306 pass**; SPA UAT trip-map
  **66/66** + event-player **38/38**. **PROVEN LIVE 2026-06-25** (device in-car,
  Sentry ON, recording untouched): `GET /api/clips/3/stream` (`ro_usb`) → **206**
  video/mp4 + `HEAD` 200 `content-length:49192` (gate passed: on-disk == catalog),
  driven through the real SPA `/events?clip=3` (HEAD-probe 200, player chrome +
  all 6 angle controls render, 0 console errors). Archive regression clean (clip 6
  real 60 MB footage decodes 2896×1876, playback advances 0→1.49 s). Perf TTFB
  368 ms / FCP 980 ms / load 1.2 s. Deployed webd+SPA only (gadgetd/USB/recording
  never touched; zero USB events in the deploy window). *Residuals (deferred):*
  a substitute file identical in BOTH vdl AND dlen passes the size gate (needs a
  content fingerprint at ingest); a >16 MB clip can truncate mid-stream after
  headers (errors the body, never serves wrong bytes as complete). Evidence:
  `files/hw-results.md`, `hw-clip6-desktop-1280.png`, `hw-clip6-mobile-375.png`.
  *Live limitation:* per-clip All-Clips controls + map pin-flash not exercisable
  on-device (catalog has 0 events / 0 GPS fixes); covered by UAT 66/66. The
  `ro_usb` test clips are 49 KB synthetic stubs (don't decode — data, not code;
  streaming/gate wiring proven 206/200).
- [x] Switch camera angle (position preserved where possible). **(proven)**
- [x] Navigate clips within an event (prev/next). **(A6b proven)**
- [ ] Telemetry HUD overlay (SEI: speed/gear/brake/throttle/steering/AP-FSD), synced. **(partial: client-side SEI parse exists — A7; verify full HUD)**
- [x] Download single angle + download whole event as ZIP. **(A8 proven — single-angle "Download Angle" + whole-clip "Download All" ZIP UI in EventPlayer; webd `GET|HEAD /api/clips/:id/export` + `/api/clips/:id/angles/:camera/download`; event-player.spec.ts "downloads —" happy-path + ro_usb disabled/inert tests, 28 passed)** **(A7 2026-06-26 — V1 Preparing→Downloading…→reset cosmetic feedback added to BOTH buttons w/ re-entry guard; live-verified bundle `index-cXtV-Iin.js`, see `files/hw-results.md` §A7)**
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
- [x] Upload chime(s) `.wav` (+`.mp3`→WAV), ≤1 MB & ≤5 s; added to `Chimes/`. **(DONE
  2026-06-18 — single-file `.wav` install into `media.img` `Chimes/` LIVE-PROVEN 2026-06-15;
  multi-file batch SHIPPED 2026-06-18; and the **client-side audio editor** (commit `22bd73e`)
  now closes mp3→WAV transcode + trim + normalize: selecting a single file opens an in-browser
  editor (Web Audio API) that decodes any format, trims, optionally LUFS-normalizes, and
  re-encodes to a 16-bit PCM WAV frame-clamped ≤1 MiB before POST — the backend contract is
  unchanged. Device gate 20/20 both viewports, console clean, auto-fit-on-decode trims an
  oversize source to 1.00 MiB → Ready. See `files/hw-results.md` "Chime audio editor".)**
- [x] Delete library chime. **(SHIPPED — optimistic "Removing…" + bounded poll
  (2 s/45 s) until the chime leaves the catalog, then auto-drop + "removed" notice,
  no manual refresh; timeout → "Refresh now"; per-entry budgets for concurrent
  deletes + synchronous double-click guard. GPT-5.5 design + diff review. Live on
  hardware: 2 chimes deleted via the real UI — optimistic state at ~160 ms, row
  auto-removed with notice at ~11 s, gone from `/api/chime-scheduler`, active
  LockChime.wav untouched, console clean — `files/hw-delete-notice-1280.png`,
  `files/hw-results.md`.)**
- [x] Rename a chime (v1 rename API). **(DONE 2026-06-18 — inline rename in the
  Lock Chimes library: webd reads the source bytes and enqueues an InstallFile of the
  destination copy (mirrors `move_music`), the SPA deletes the source via
  `DELETE …/library/{name}?cascade=false` after the copy converges, and schedulerd
  rewrites every reference — `schedule.chime_filename` and `group.chimes` (case-insensitive
  match, verbatim write, case-insensitive member dedupe). Validation: bad `from`→404,
  bad `to`→400, case-only same-name→400, dest exists→409. Companion **delete cascade**:
  deleting a library chime now removes dependent schedules, scrubs groups, deletes emptied
  groups, and resets `random_mode` if it pointed at a deleted group (default `cascade=true`;
  `bulk_delete` cascades on sanitized basenames so file-op and cascade agree). Verified:
  podman `cargo test -p teslausb-core -p schedulerd -p webd` green (schedulerd 39, webd 274,
  core 343; new tests incl. `rename_canonicalizes_existing_target_case`,
  `bulk_delete_library_cascades_sanitised_basenames`, schedulerd cascade store/ipc tests),
  clippy clean on touched code, GPT-5.5 adversarial review reconciled (2 findings fixed +
  regressions). Playwright UAT green — 56/56 both viewports incl. rename cascade convergence,
  inline-editor visual+perf, client validation, delete cascade; rename screen ready ~207 ms,
  FCP 96 ms, clean console — `spa/test/uat/chime-scheduler.spec.ts`,
  `spa/test/uat/artifacts/chime-rename-{desktop-1280,mobile-375}.png`,
  `perf-chime-rename-*.json`.)**
- [x] Edit / re-trim a library chime (V1 parity — A6). **(DONE 2026-06-26 — V1's
  `editChime()` reopens the trim editor in edit-mode: badge "Editing: <file>", a unique
  `_edited` name suggestion, loads the existing chime audio, re-uploads as a NEW library file
  (original untouched). Added per-row `Edit` button (`data-testid=library-edit`, order
  Download→Edit→Set Active) + `onEditChime` prop in `ChimeScheduler.tsx`; `Media.tsx` fetches the
  chime via `api.libraryDownloadUrl`, builds a `File` named by `suggestEditedName` — a faithful
  port of V1's `generateUniqueFilename` (horn→horn_edited→horn_edited2…; strips/increments an
  existing `_editedN`; full-filename collision check), opens the editor through a token-guarded
  race-safe handler (edit/file-pick/cancel supersede a pending fetch); `editingLabel` badge in
  `ChimeAudioEditor.tsx`; badge CSS in `media.css` mirroring V1's green pill. SPA-only, no backend
  change. GPT-5.5 review found 3 issues (V1-parity name algo, `res.blob()` outside try, in-flight
  race) → all fixed → focused re-review PASS. `npx playwright test chime-scheduler.spec.ts
  chime-editor.spec.ts` 68/68 strict both viewports (+2 parity cases: `horn_edited`,
  `horn_edited2`). Deployed to device (SPA static-dir swap, bundle `index-BLs6wbA5.js`); live
  Playwright edited real `EngineRev.wav` → editor opened with decoded audio, badge
  "Editing: EngineRev.wav", suggestion `EngineRev_edited`, 0 console errors / 0 non-2xx, no upload.
  See `files/hw-results.md` §A6.)**
- [x] **Set active** → copy library file to media-root `LockChime.wav`, applied
  **immediately** (per-partition hot-handoff on P2; no manual replug); UI shows which
  library chime is active. **(DONE 2026-06-15 bench-proven — `POST …/library/{name}/activate`
  re-validates the WAV and installs it to media-root `LockChime.wav` via the gadgetd
  queue; the new gadgetd applies P2 handoffs immediately while the host is enumerated.
  LIVE: media-root `LockChime.wav` updated byte-identical to the activated chime in ~3 s,
  no replug — `files/hw-results.md` "Phase 2". Car-side pickup (soft medium-change vs.
  full re-enumeration) is the remaining §1.1 / C1 car-only check. **UI auto-refresh
  LIVE-PROVEN 2026-06-16: Set Active updates the Active Lock Chime card + audio player
  with no manual reload (parent bounded-polls `/api/chimes`); clear in-flight/converged/
  timeout notices; buttons disabled while pending. See top "Resume here — SET-ACTIVE UI
  AUTO-REFRESH" block + `files/hw-setactive-*.png`.)** **Active card SOURCE-NAME
  LIVE-PROVEN 2026-06-15: the card now shows the selected library chime's name (e.g.
  `MarioFart.wav`) with an "Installed as LockChime.wav" subtitle, resolved client-side
  (just-activated name, else unique library size-match, else honest `LockChime.wav`
  fallback). Verified on hardware cold-load + in-session activation, console clean —
  `files/hw-sourcename-1280.png`.**
- [x] Groups (create/edit/delete; persist scheduler state). **(CRUD round-trip VERIFIED
  2026-06-18 — schedulerd `update_group_persists_across_reload` (write→reload-from-disk
  assert) + UAT "edit group"/"delete group" drive the real webd through PUT/DELETE on
  desktop+mobile, console clean. NOTE: B-1 consolidates v1's separate `chime_groups.json`
  into one schedulerd state file, persisted atomically on every mutation.)**
- [x] Schedules (weekly/date/holiday/recurring; CRUD+enable; `chime_schedules.json`). **(CRUD+UI
  proven; **now ENFORCED** via the webd enforcer — A3d DONE 2026-06-17, live-proven on hardware)**
- [x] Random mode from a group (`chime_random_config.json`); rotates active chime. **(model+UI
  persist proven; **now ENFORCED** — random-on-boot live-picked EngineRev from group "Fun" on the
  device, see A3d.2/A3d.4 + `files/hw-results.md`)**

#### A3d · Enforcement loop — make schedules & random mode actually change the car's chime

> **Why this exists.** Verification 2026-06-17 confirmed the chime *config/state* layer
> is fully functional (CRUD persists + serves), but **no schedule or random-mode setting
> ever changes `LockChime.wav` on the car** — the actuation layer is unbuilt. The
> `Evaluate` IPC command (`schedulerd/ipc.rs`) has **zero callers**, `store.evaluate()`
> omits `random_mode`/`groups`, `Interval::OnBoot` returns `None` from `trigger_today`,
> and nothing enqueues the gadgetd `LockChime.wav` swap on a fired rule.
> **No longer gated on F4/F5** — both are `[x]` done and the **Set Active** path already
> swaps `LockChime.wav` via the gadgetd queue, live-proven (§4.5 "Set active"). A3d reuses
> that proven path on a timer. Only the car-side re-enum (A3d.5) is Tier-C/`gated:C6`+§1.1.

> **DONE 2026-06-17 — enforcement built, Linux-validated, and LIVE-PROVEN on hardware.**
> Architecture decision (vs. the original A3d.1/A3d.4 wording): the per-minute tick + boot
> hook live in **webd** (`chime_enforcer.rs`), which already owns the gadgetd activate path,
> the media-backed library, the schedulerd client, and a tokio runtime. **schedulerd stays a
> pure state/decision owner** — it gained an `Evaluate{library}` + `EvaluateBoot` IPC; core
> gained `resolve_boot`. Enforcer is guarded by `WEBD_CHIME_ENFORCER=1` (tests never fire).
> Spec: `docs/specs/schedulerd.md` (GPT-5.5 design + diff + deploy reviews reconciled).
> Linux: core 343 / schedulerd 29 / webd 259 green; clippy-clean on new code.
> **Hardware (cybertruckusb.local, see `files/hw-results.md` "Lock Chime enforcement (A3d)"):**
> schedule path installed tesla-tron **byte-identically** (`7c1dfc0c`, 231730 B) at the fired
> trigger; random-on-boot picked **EngineRev** (505564 B) from group "Fun" on reboot; **no
> churn** (stable mtime, zero gadgetd handoff spam). Active chime lives on the gadget loopback
> partition `/run/teslausb/media-ro/LockChime.wav` (loop0p1 exfat) — NOT the stale
> `/srv/teslausb/media/` mirror. Shipped **enabled** (`WEBD_CHIME_ENFORCER=1` in `webd.service`).

- [x] **A3d.0 · schedulerd-enforcement spec.** `docs/specs/schedulerd.md` written; GPT-5.5
  design review reconciled (Some(empty) library authoritative; tick `active_chime=None`;
  empty-skip; membership guard). Defines tick cadence, evaluate→activate contract, idempotency,
  random-on-boot, `OnBoot` handling.
- [x] **A3d.1 · Enforcement tick (in webd, not schedulerd).** `chime_enforcer.rs` `enforce_tick`
  runs per-minute (+ once at boot) calling schedulerd `Evaluate`; guarded by `WEBD_CHIME_ENFORCER`.
  Green host unit tests; LIVE-PROVEN no-churn on device.
- [x] **A3d.2 · `random_mode` + `groups` in the evaluator.** schedulerd `store.evaluate_boot()` +
  `random_members()` (group `chimes` ∩ library); `resolve_boot` in core picks deterministically
  per `boot_seed`. LIVE: random-on-boot picked EngineRev from group "Fun".
- [x] **A3d.3 · Activation enqueue on pick-change.** `install_library_chime_as_active()` reuses
  the proven Set-Active gadgetd write path; idempotent vs in-memory `last_enforced` (1 handoff
  per pick change). LIVE: no thrash over a 90 s soak.
- [x] **A3d.4 · Boot-time hook.** webd `enforce_boot()` at startup evaluates `EvaluateBoot`
  (schedule-at-boot **beats** random; random pick when no eligible schedule + `random_mode` on).
  core `resolve_boot` + `trigger_today_boot` handle `OnBoot`. LIVE: EngineRev installed on reboot.
- [x] **A3d.5 · Car pickup of an active-chime change.** A `LockChime.wav` swap needs the
  **full USB re-enumeration** path (§1.1 #2 / `tesla_gadget_rebind.sh` behavior), not the
  soft medium-change. **DONE — gadgetd i2 auto-fires the re-enum after any `LockChime.wav`
  install (incl. the enforcement/Set-Active path); HW-PROVEN & operator-confirmed
  2026-06-24: car re-reads + plays the new chime, recording intact (`files/hw-results.md`,
  spec [`gadgetd.md` §9](specs/gadgetd.md)).**
- [x] **A3d.6 · Tests + proof.** Host unit/integration green (core/schedulerd/webd); **Playwright**
  46/46 chime-scheduler UAT green on desktop-1280 + mobile-375 (perf, clean-console, wiring,
  screenshots); **hardware-test** proved schedule + random-on-boot swap `LockChime.wav` with no
  churn. Evidence: `files/hw-results.md`, `spa/test/uat/artifacts/chime-scheduler-{desktop,mobile}*.png`.

#### A3e · Device timezone setting + DST-correct enforcement — DONE (deployed + live-proven 2026-06-18; A3e.4 no-RTC gate also DONE + live-proven 2026-06-18)

> **Why this exists.** The enforcer evaluated schedules against a **fixed** UTC offset:
> `chime_enforcer::local_offset_secs()` reads `WEBD_TZ_OFFSET_SECS` (a fixed value) BEFORE
> the DST-aware `date +%z` fallback, and production shipped a hardcoded
> `WEBD_TZ_OFFSET_SECS=-14400` device drop-in — **defeating DST** (chimes would fire an hour
> off across a DST boundary). Root fix: **drop the fixed prod override** so the offset is
> derived DST-aware from the system tz, and add a **web UI to set the system timezone**
> (`timedatectl set-timezone`). `webd` runs as root today, so it can set the system tz directly;
> a future non-root transition swaps the injectable `TimezoneSetter` impl.
> **Pi Zero 2 W has NO RTC / clock battery / fake-hwclock** → time is NTP-only at boot; a
> no-network boot evaluates schedules against a bogus clock (separate follow-up **A3e.4**).
>
> **Working-tree state (UNCOMMITTED at 2026-06-17 pause):** new `webd/src/timezone.rs` +
> `lib.rs`/`route.rs`/`tests.rs` + SPA `api/{types,client}.ts` + `screens/ChimeScheduler.tsx`
> + `test/uat/{global-setup,chime-scheduler.spec}.ts` + `deploy/systemd/webd.service`.
> Host: `cargo test -p webd` **260 green**, `timezone.rs` clippy-clean, `tsc` clean,
> **Playwright `chime-scheduler` 48/48** (desktop+mobile, console-clean). GPT-5.5 round-1
> reconciled; **round-2 re-review (`tz-rereview`) was in flight at pause — read it first.**

- [x] **A3e.1 · `GET/PUT /api/system/timezone` (webd).** New `timezone.rs`: GET →
  `{current: string|null, zones[]}` (allow-list enumerated from TZif-magic files under the
  zoneinfo base; excludes `posix/`, `right/`, `posixrules`, `*.tab`/`*.zi`/`*.list`,
  leapseconds, etc.). PUT validates the requested zone against the **fresh** allow-list (422
  `invalid_timezone` on unknown; rejects empty/NUL/`..`/leading-`/`) then `timedatectl
  set-timezone <zone>` (args passed directly, **no shell**); 500 `timezone_set_failed` on
  failure. Blocking work (subprocess + ~600-file fs walk) runs in `spawn_blocking`; injectable
  `TimezoneSetter` trait + pure `put_timezone_with` core; `WEBD_ZONEINFO_DIR` test override.
  `enumerate_zones` hardened against symlink escape (`symlink_metadata` classification +
  canonical-base confinement; Unix-gated test, Linux-validated via podman). **(LIVE-PROVEN
  2026-06-18: `GET` 200 with 486 zones incl. America/New_York, posixrules/zone.tab excluded;
  `PUT` round-trip America/New_York↔America/Detroit; invalid `../etc/passwd` → 422. See
  `files/hw-results.md` "A3e.3".)**
- [x] **A3e.2 · "Device timezone" selector (SPA).** ChimeScheduler screen: loads zones on
  mount, shows `current` selected, PUTs on change, error + dismissible success notice, and
  **hides gracefully** when `zones` is empty / GET fails (degrades, never throws). **(Playwright
  chime-scheduler 48/48 desktop+mobile, console-clean; LIVE-PROVEN on hardware 2026-06-18:
  selector renders America/Detroit on real page, console clean, mobile-375 + desktop-1280
  screenshots in `files/tz-deploy/`.)**
- [x] **A3e.3 · Deploy + DST-correct enforcement proof (hardware).** Cross-built aarch64 webd
  (podman debian:bookworm, 1.85.0; sha d35adc81…); on `cybertruckusb.local` **REMOVED
  `WEBD_TZ_OFFSET_SECS=-14400`** from the device drop-in (kept `WEBD_CHIME_ENFORCER=1`); deployed
  webd + SPA under hardware-test rails (backups + atomic swaps + dead-man). **VERIFIED:** offset
  env absent in webd process; `date +%z=-0400` DST-aware via system tz (America/Detroit); enforcer
  `chime_enforcer.rs` byte-identical to proven 617bce7; enforcement actuates (active LockChime set
  at boot time by random-on-boot); survived a clean reboot (is-system-running=running, no failed
  units, SSH/WiFi up). **(DONE — see `files/hw-results.md` "A3e.3".)**
- [x] **A3e.4 · No-RTC clock-plausibility gate (follow-up, high priority).** Pi has no RTC; the
  enforcer now **skips time-based enforcement until the clock is NTP-synced/plausible**.
  Plausibility is **fail-closed**: trustworthy requires year ≥ 2024-01-01Z floor **AND**
  `timedatectl NTPSynchronized=yes`; missing/garbage/`no` → not plausible (`WEBD_CLOCK_PLAUSIBLE`
  env override is the escape hatch). Clock trust is decided in **webd** (owns the real clock);
  core resolvers stay pure. **Tick path** (schedule-only, no random fallback) is skipped entirely
  when implausible, logging once per skip-transition (latch resets when plausible again).
  **Boot path** is NOT short-circuited — clock-INDEPENDENT behaviors still fire: random-on-boot
  AND `Interval::OnBoot` recurring schedules (eligible at minute 0); only time-WINDOWED schedules
  (weekly/date/holiday/timed-recurring) are skipped via `schedulerd.evaluate_boot_clockless`
  (filters to OnBoot-recurring only — NOT an empty list). `clock_plausible: Option<bool>` is
  `#[serde(default)]` on `EvaluateBoot` only (backward-compat; omitted/None preserves behavior).
  **(DONE — coded by GPT-5.3-codex to an Opus-authored spec, GPT-5.5 adversarial code + deploy-plan
  reviews reconciled (2 code fixes: boot-path pre-epoch `?` short-circuit → `0` fallback; skip
  `timedatectl` when year floor already fails). Tests: core 343 / schedulerd 32 / webd 265 all pass
  incl. 5 new (`plausibility_from_truth_table`, `parse_plausible_override_truth_table`,
  `evaluate_boot_clock_implausible_skips_schedule`, `evaluate_boot_clockless_skips_time_windowed_schedules`,
  `evaluate_boot_clockless_honors_onboot_recurring_schedule`); schedulerd clippy clean.
  LIVE-PROVEN on hardware 2026-06-18: cross-built aarch64 webd+schedulerd, atomic-mv deploy under
  dead-man rails; with `WEBD_CLOCK_PLAUSIBLE=0` the device logged the boot + tick skip lines (once),
  the active LockChime stayed unchanged — time-windowed "Evening Chime" schedule correctly skipped
  while random-on-boot preserved — and normal enforcement resumed cleanly after removal; no reboot,
  no failed units, SSH/WiFi up. See `files/hw-results.md` "A3e.4" + `files/hw-a3e4-clockgate-journal-*.log`.)**

### 4.6 Music — `Requirements.md` §4.6

- [x] Browse library incl. nested folders. **(DONE — client-derived folder tree from
  `GET /api/music` rel_paths; breadcrumb + per-folder Files view. LIVE-PROVEN on hardware
  2026-06-16: created `/Music/zz-deploytest`, navigated in (breadcrumb `/Music / zz-deploytest`,
  upload target scoped to the folder), navigated back to root — console clean. See
  `files/hw-results.md` "music-rework".)**
- [x] Play track in-browser (native `<audio preload="none">` per row, streamed from
  `GET /api/media/content?path=<rel>&v=<mtime>`). **(DONE — read path covered by
  webd range-streaming integration tests; SPA wiring verified `npx playwright test
  music.spec.ts` incl. mocked-list player test asserting preload=none + encoded src +
  cache-bust + NO content-fetch on render; 14 passed, clean console/network)**
- [ ] Upload `.mp3/.flac/.wav/.aac/.m4a`, up to **2 GB**, **16 MB chunked** upload. **(gated: chunked-upload backend — Tier-C remainder A1/A2)**
  - **(2026-06-17: drag-and-drop + multi-file selection now work.** The drop zone was a
    styled div with no DnD handlers and a single-file input. Wired real
    `onDragOver/Enter/Leave/Drop` (reads `dataTransfer.files`, `.dragging` highlight) +
    `multiple` on the input; `useMusicLibrary` now stages a `File[]`, filters to the allowed
    extensions client-side, and uploads sequentially with `Uploading n/m…` progress (gadgetd
    coalesces the handoffs). SPA-only; ≤10 MB single-shot cap unchanged. `npm run build`
    clean; `npx playwright test music.spec.ts` 30 passed (desktop-1280 + mobile-375) incl. a
    real-`DragEvent` drop-of-two-files test + a `multiple`-attribute assertion; console clean.)**
- [x] Create folders + move files between folders. **(DONE — folder create = install a
  `.teslausb-keep` placeholder (folder derived client-side, dotfile hidden); folder delete =
  media-ro filesystem-walk enumerate child files + chunked (≤16) `run_remove_many`; move =
  copy-only `musicMove` then FE two-phase source-delete after dest converges (no data-loss
  window). `cargo test -p webd` 251 passed; `npx playwright test music.spec.ts` 26 passed on
  desktop-1280 + mobile-375 incl. create/navigate/two-phase-move. LIVE-PROVEN on hardware
  2026-06-16: create folder auto-refreshed with confirmation notice; upload into the folder
  appeared immediately (no manual refresh); full native `<audio>` control bar per row. See
  `files/hw-results.md` "music-rework".)**
- [x] Delete files (and folders). **(DONE — per-row + bulk file delete sends STRIPPED
  subpaths (fixes the `Music/Music/...` double-prefix that made delete silently no-op);
  recursive folder delete enumerates child files via the media-ro walk. Auto-refresh polls
  until the row is absent. LIVE-PROVEN on hardware 2026-06-16: deleted a file (row
  self-removed, Files→0) then deleted the folder (root returned to empty) — `/api/music`
  `{"items":[]}`, webd NRestarts=0, console clean. See `files/hw-results.md` "music-rework".
  **FOLLOW-UP (2026-06-16): orphaned empty directory on the USB now actually removed.** Added
  a gadgetd `RemoveEmptyDir` eject-handoff mutation (empty-only `remove_dir`, walks up,
  refuses protected/TeslaCam roots + symlinked path components); folder-delete enqueues the
  file deletes then the prune (best-effort, ordered after deletes, own handoff). Repairs
  already-orphaned empty folders too. gadgetd 99 + webd 254 tests pass; GPT-5.5 reviewed
  (2 blockers fixed: intermediate-symlink canon==lexical guard, symlinked-folder refusal).
  LIVE-PROVEN on hardware: created `HWTEST_DELME` → deleted → directory gone from the live
  exFAT (`/run/teslausb/media-ro`); two pre-existing orphaned empty dirs (`test`,
  `zz-deploytest`) deleted via folder-delete and removed. gadget never torn down
  (control-daemon-only restart), NRestarts=0. See `files/hw-results.md` "folder-delete".)**
- [x] Rename a file during move (V1 parity — A5). **(DONE 2026-06-26 — V1's move dialog
  prompts a destination folder THEN an optional new filename ("leave blank to keep name"); B-1's
  move dialog had a destination select only. Added an optional filename input
  (`data-testid=music-move-newname`, "Keep original name" placeholder) + destName algorithm in
  `useMusicLibrary.onConfirmMove(dest, newName?)`: blank/whitespace → keep original; typed value
  with a music extension → use as-is; typed value without one → append the SOURCE file's extension
  (so webd's `check_extension` doesn't 422); path-traversal stripped via `split(/[\\/]/).pop()`.
  Pure SPA change — `move_music {from,to}` already supported rename (the dest basename is `to`'s
  last component). 3 files: `useMusicLibrary.ts`, `Music.tsx`, `music.spec.ts` (move test
  parameterized into 3 cases asserting the `to` payload: renamed.mp3 / track.mp3 / song.wav).
  GPT-5.5 reviewed clean. `npx playwright test music.spec.ts` 34/34 strict (zero-console). Deployed
  to device (SPA static-dir swap, bundle `index-1Qwn-Imu.js`); live Playwright proved the live JS
  emits `to:"DaftPunk/renamed.mp3"`, bundle actually loaded, 0 console errors, no real file moved.
  See `files/hw-results.md` §A5.)**
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
- [x] Delete files/shows (incl. bulk). **(bulk-delete proven)**
- [ ] **FOLLOW-UP (pre-existing layout bug, found during the shell-parity lane):**
  `spa/test/uat/light-shows.spec.ts:262` ("row checkbox sits in its own column, not over the
  show name") FAILS at **mobile-375** — the select checkbox renders at x≈332 (right side) instead
  of before the name (x≈43). Confirmed pre-existing (reproduces on baseline `Shell`, unrelated to
  the health-dot change). Desktop-1280 passes. Needs a `light-shows.css` row-grid fix so the
  checkbox column precedes the name at narrow widths.

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
- [ ] **OS-starvation guard (binding — see invariant 6): hard, always-enforced
  OS/app reserve the archive can NEVER consume.** The card's `/`, `/data`, and the
  ext4 archive share one filesystem, so unbounded archive/thumbnail/SQLite/upload-
  staging/log growth can make the device fail to boot or crash. The storage
  governor MUST keep free space above a sacrosanct floor (`storage.md §2` tier-1,
  `max(5%, 2 GiB)` on shared-fs) — evicting archive oldest/lowest-value first and
  denying new write budget *before* the OS is endangered, even when archive usage
  alone looks fine. Not optional; independent of LUN sizes. **(gated:B5)**
- [ ] Cleanup tuning: target free %, Sentry max-age, preserve-GPS-clips. **(gated:B1/B2)**

### 4.12 Storage Health — `Requirements.md` §4.12

- [ ] View health: mount status, FS error counts, SMART/health severity, alerts. **(partial:
  Linux/Pi-gated probes — A5; archive-worker progress-freshness subsystem landed +
  live-verified 2026-06-26 — webd emits a `worker` block ("Idle, queue empty" /
  "{n} pending" / "{n} pending (catch-up)" / "{n} pending — not draining" /
  "Worker heartbeat stale" / "Worker not running") fed by a retentiond heartbeat
  at `/run/teslausb/retentiond.health.json`; renders as the "Background Worker" row.
  Evidence: `files/hf-live-desktop.png`, `files/hw-results.md`, sysinfo.rs worker_block tests.
  Indexer-liveness subsystem (Slice 2) landed + live-verified 2026-06-26 — webd emits an
  `indexer` block ("Indexer healthy" / "Indexer stalled" / "Indexer not running" /
  "Indexer status unavailable") fed by an indexd process-liveness heartbeat at
  `/run/teslausb/indexd.health.json`; drives the "Video Indexer" row dot from real
  liveness while the row message stays the exact V1 catalog text ("N clips indexed;
  newest is M d old") when healthy and composes "{reason} — {catalog}" when degraded.
  Live wiring proof injected indexer:warn → dot flips warn + composes "Indexer
  stalled — 26 clips indexed; newest is 0 d old" on served bundle index-4sS_cP36.js,
  0 console errors. Also fixed a latent age-subtraction overflow in both worker_block
  and indexer_block (saturating_sub).
  Evidence: `files/s2-live-proof-warn.png`, `files/hw-results.md`, sysinfo.rs
  indexer_block tests, media-hub UAT indexer fallback matrix)**
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
- [ ] Map/display prefs (units, timezone) + network settings. **(partial: map display prefs now persist via `PUT /api/settings` — speed unit + local/UTC clock, see §4.1; network settings + dedicated Settings screen still pending)**
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
- [x] **C3 · Real Tesla footage validation** (replace synthetic SMPTE). **PASSED
  2026-06-23 (device in-car, Sentry ON, real footage).** C3 caught that the Phase-1
  archive pipeline — green only on 48 KiB synthetic stubs — failed on every real
  multi-MB clip (`registered=0`): root cause indexd rejected real `left_pillar`/
  `right_pillar` cameras (allowlist only had bogus `left`/`right`), secondary the 5 s
  read timeout under microSD write-contention. Fixed (camera allowlist + 60 s read
  timeout + Rejected/Error taxonomy + rejected-key tombstone), cross-built aarch64
  via podman, deployed indexd+retentiond under hardware-test rails. Result: real
  RecentClips now archive (9 LIVE items, ~1.49 GB) and the **archive-then-decodes**
  proof for §4.2 #2 passes (206 + Chromium decode, 0 dropped frames). Full-duration
  (59.8 s) decode also demonstrates scannerd reads real footage faithfully (no
  truncation/clamp gap → VDL-clamp concern satisfied). See `files/hw-results.md`. **(C)**
- [ ] **C4 · Push held commits + port-80 + live deploy.** **(C)**
- [ ] **C5 · Security ruling on rclone-key write exposure** (blocks B3 config-write). **(C)**
- [ ] **C6 · Car change-propagation verification** (§1.1 soft vs full re-enum). **(C)**
  **(full re-enum / chime half DONE — HW-PROVEN 2026-06-24, `files/hw-results.md`. Remaining:
  the soft medium-change half for directory listings.)**

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
   chime enforcement loop (A3d — bench-unblocked now F4/F5 are done; reuses the
   proven Set-Active gadgetd path; only A3d.5 car re-enum is Tier-C/C6) · Failed
   Jobs richness (A8/B7).**
8. **Remaining Tier-C at-vehicle (after C1):** migration F1 → calibration C2 →
   real-footage C3 → change-propagation C6.

> Update this file as the source of truth: tick a box only after a tested-successful
> run, and link the evidence (Playwright report / `files/hw-results.md` entry).
