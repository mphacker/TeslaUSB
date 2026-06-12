# SPEC — `scannerd` (R1 raw exFAT/MP4/SEI reader)

> Parent: [`SPEC.md`](./SPEC.md) · Criticality: disposable · Language: Rust
> Implements the resolved read path (R1) from `docs/plan.md`: a conservative
> raw userspace parser that **never mounts** the Tesla filesystem.

## 1. Objective

Read the car's recorded media for indexing **without ever mounting** the Tesla
filesystem read-write or interfering with the car's writes. `pread()` the raw
backing (the image file / loop device, or a short-lived raw snapshot for explicit
export), parse `MBR → exFAT → FAT chain → MP4 → H.264 SEI`, and emit records only
for files that are provably **stable**.

## 2. Responsibilities

1. **Raw traversal:** read the two backing image files (`teslacam.img`=lun.0,
   `media.img`=lun.1) — parse each MBR, locate its single exFAT partition, and
   walk the directory tree and cluster chains by reading bytes directly. No
   kernel exFAT mount of the *live* TeslaCam volume. (scannerd produces the
   **catalog/metadata** for both volumes; media *byte* reads are served by webd
   over gadgetd's RO `media.img` mount, not by scannerd — see §2.6 / ADR-0003.)
2. **Concurrency tolerance (stability gating):** the car may be writing
   concurrently. Only emit a clip/file as "ready" when its **directory entry +
   cluster chain + MP4 box tail (moov/`mdat` completeness)** are **stable across
   two or more scans**. Anything in flux is skipped and retried later
   (skip-and-retry). **No false "stable".**
3. **SEI extraction:** for stable MP4s, locate and decode the **H.264 SEI
   telemetry** (`user_data_unregistered`, `payload_type=5`, reusing the existing
   Rust SEI parser in `teslausb-core`) into structured per-frame/per-time samples.
   Detect the codec from the sample description (`avcC` vs `hvcC`) rather than
   assuming it, so a future HEVC variant fails loudly instead of silently
   mis-parsing.
4. **Clip grouping:** group the angle videos recorded together (all `*.mp4` files
   sharing one `YYYY-MM-DD_HH-MM-SS` timestamp — **whatever** camera set the model
   produced; never assume front/back/left/right) into a logical **"clip"**,
   matching today's behavior.
5. **Emit** parser output to `indexd` (records: file identity, timestamps,
   partition, clip grouping, SEI sample stream, event hints) over a local IPC /
   queue. `scannerd` derives nothing about trips/events — it only produces facts.
6. **Live-clip content-read fallback (`ReadFile`, lun.0 only — planned/deferrable).**
   In addition to metadata, optionally serve **TeslaCam clip bytes on demand** for
   the trip-map player when a clip is **not yet in the Pi-side ext4 archive**.
   Resolve `{ path }` on `teslacam.img` through the same raw `MBR → exFAT →
   FAT-chain` walk and return the requested `[offset, offset+len)` window, no-mount
   and stability-aware (catalog-`stable` clips only, clamped to
   `valid_data_length`, identity-fenced, `410` on change). Map playback is
   **archive-first**, so this fallback can be built *after* the archive loop is
   proven. **Media bytes are NOT served here** — music/boombox/lightshow audio,
   wrap/plate thumbnails, and the Active Lock Chime player are served by `webd`
   reading through `gadgetd`'s read-only loop-mount of the static `media.img`
   (simpler, proven). See
   [`contracts/scannerd-readfile.md`](./contracts/scannerd-readfile.md) and
   [`ADR-0003`](../adr/0003-media-read-path.md).

## 3. Non-responsibilities

- No writes, ever. No mount, ever (RW or RO of the live Tesla FS).
- No trip/event derivation, no DB schema ownership (that is `indexd`).
- No transcoding, no thumbnail generation of full video (thumbnails, if any, are
  cheap keyframe stills produced downstream and capped). `ReadFile` returns raw
  bytes only; any resize/transcode is a downstream concern.

## 4. Consistency model

- **Default path:** parse the live image/loop with stability gating. Best-effort,
  eventually-consistent; conservative by design.
- **Explicit export/playback path (optional):** when a fully consistent
  point-in-time view is required, take a **short-lived, hard-time-limited** raw
  block snapshot and parse it **with this same raw parser** (never a kernel exFAT
  mount, never dm-thin, never an unbounded snapshot). Release the snapshot
  promptly.

## 5. Acceptance criteria

- [ ] Indexes all stable clips on p1 with correct timestamps and angle grouping.
- [ ] While the car is actively recording, never emits a torn/incomplete file as
      stable (verified against a writer-simulating fixture).
- [ ] Extracts SEI telemetry matching known-good fixtures byte-for-byte at the
      sample level.
- [ ] Runs within its `MemoryMax` cap; streams, never loading whole files.
- [ ] Pi read I/O never starves car writes (honors ionice/IOWeight; unknown #5).

## 6. Testing

- Fixture-based tests with recorded raw exFAT/MP4/SEI byte images (clean and
  mid-write-torn variants) asserting the stability gate and SEI output.
- Property test: random interleavings of "writer appends/rewrites dir entry"
  must never yield a false-stable result.
- Memory-bound test: large image streamed under the cap.

## 7. Boundaries

**ALWAYS** read raw; gate on cross-scan stability; stream within the memory cap;
honor I/O priority so car writes win. For `ReadFile`, **jail** the requested path
inside the named partition (reject `..`/absolute escapes), cap the byte window, and
stream — never load a whole file.
**ASK FIRST** before adding any snapshot mechanism beyond the short-lived raw
snapshot, or before changing the stability heuristic.
**NEVER** mount the Tesla FS; never write to it; never use dm-thin or an
unbounded snapshot; never block or slow the car's writes.
