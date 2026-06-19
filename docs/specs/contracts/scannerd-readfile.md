# Contract — `scannerd` live-clip read fallback (`ReadFile`, lun.0 only)

> Parent: [`scannerd.md`](../scannerd.md) ·
> [`usb-io-and-archiving-architecture.md`](../usb-io-and-archiving-architecture.md) §0.1 ·
> [`ADR-0003`](../../adr/0003-media-read-path.md)
> Status: **ACTIVE — REQUIRED FOR PHASE-1 ARCHIVING (2026-06-19, ADR-0004)**.
> Opus design, reconciled with GPT-5.5 adversarial reviews (ADR-0003/0004).
> No longer deferred: this socket is the **byte source for `retentiond`'s
> archive copy** (the Pi must never mount `teslacam.img`, so the archive loop
> reads clip bytes through this seam). `webd`'s live-clip map fallback (§7) is
> still a later task, but the socket + protocol below ship now for `retentiond`.
> **Clients:** `retentiond` (archive copy — primary, now) and `webd` (live-clip
> fallback — later).

## 1. Why (and why this is now small)

**Media bytes are NOT served by this seam.** Per [`ADR-0003`](../../adr/0003-media-read-path.md),
media audio, wrap/plate thumbnails, and the Active Lock Chime player are served
by `webd` reading through `gadgetd`'s **read-only loop-mount of the static
`media.img`** with `std::fs`. No custom byte-server is involved for media.

This seam exists for **one** remaining case: playing a **recorded TeslaCam clip
on the trip map** when that clip is not yet in the Pi-side ext4 archive. Map
playback is **archive-first** — `webd` serves the durable archive copy whenever
it exists. Only the recent, not-yet-archived window needs a live read of
`teslacam.img` (`lun.0`), which the car is writing continuously and which the Pi
must **never mount** (the #1 invariant + cache-incoherence).

Because archive-first covers the common case, **this fallback MAY be implemented
after the retentiond archive loop is proven** (see `docs/status.md` sequencing).
If archiving keeps up, the live window is small and this is purely a freshness
nicety.

`scannerd` already has every primitive needed:

- `walk::walk_volume` yields `FileRecord { path, first_cluster, data_length,
  valid_data_length, no_fat_chain, partition_slot, … }`.
- `volume::Volume::follow_chain(first, no_fat_chain, span)` → cluster list.
- `volume::Volume::read_file_range(&clusters, start_in_file, len)` → bounded
  bytes (already used for MP4/SEI; never slurps more than `len`).

## 2. Transport — a dedicated read socket

`scannerd serve` binds ONE socket (`/run/teslausb/scannerd.sock`) for the
`indexd` scan cadence; `indexd` holds that connection for its process lifetime,
so a second client on it would head-of-line block. Add a SECOND listener,
`/run/teslausb/scannerd-read.sock`, dedicated to content reads:

- Concurrent, short-lived handler per connection (`Arc<PreadReader>`; no `unsafe`,
  no shared mutable state). `ReadFile` touches **no** `StabilityTracker` and never
  writes; `PreadReader` uses positioned `pread(2)`, so concurrent handlers are
  trivially safe.
- `0o660` socket inside the `0o750` `teslausb` runtime dir; `webd` is the only
  client. The existing `scannerd.sock`/`indexd` path is untouched.

## 3. Wire protocol (`proto.rs`)

Control frames are JSON (4-byte LE length prefix, `MAX_REQUEST_FRAME = 64 KiB`).
**The data plane is raw binary** (a JSON-encoded `Vec<u8>` inflates 6–8×): a read
response is a small JSON header frame followed by a length-prefixed raw byte tail.

A **single, cheap, stateless RPC** with an inline identity handle — no separate
`Resolve` round-trip, no per-slot generation counters, no per-chunk exFAT
`SetChecksum` re-walk (all of that was cut as over-engineered; see ADR-0003).
The identity handle defeats the "wrong bytes across a file replacement" failure
**within a single HTTP request** without the heavyweight machinery.

```rust
// client → server  (JSON control frame)
pub struct ReadFileRequest {
    pub path: String,            // TeslaCam-volume-root-relative; hostile input (§4)
    pub offset: u64,
    pub len: u32,                // server caps at MAX_READ_LEN
    pub handle: Option<ClipIdentity>,  // None on the first chunk; echoed thereafter
}

/// Cheap identity captured on the first chunk and echoed by webd on every
/// subsequent chunk of the SAME HTTP request, so the response stream cannot
/// stitch bytes from two different file incarnations.
pub struct ClipIdentity {
    pub first_cluster: u32,
    pub total_size: u64,         // DataLength at first resolve
    pub name_hash: u32,          // exFAT NameHash of the resolved leaf name
}

// server → client  (JSON header frame, THEN a u32-LE-prefixed raw byte tail)
pub enum ReadFileHeader {
    Ok {
        identity: ClipIdentity,  // returned on the first chunk for webd to echo
        readable_size: u64,      // current valid_data_length ceiling
        eof: bool,               // window reached readable_size
        byte_len: u32,           // length of the raw tail that follows
    },
    Changed,                     // identity no longer matches on disk → webd returns
                                 // HTTP 410 Gone (NOT 404, NOT wrong bytes)
    NotFound,                    // path did not resolve to a live file
    OutOfRange,                  // offset > readable_size
    Error { message: String },  // no path echo
}
```

`MAX_READ_LEN = 8 MiB` per request; `webd` loops over HTTP range chunks for
larger clips, reusing the `ClipIdentity` from the first chunk across the loop.

## 4. Path resolution + jail

`path` is **hostile input**. Before any disk access:

1. Reject empty, absolute (leading `/`), any `..`/`.` component, NUL, backslashes;
   cap total length (1024) and component count (32).
2. Percent-decode first (single pass, reject a lone `%`), THEN split on `/`, THEN
   re-check no component is `.`/`..`/empty (defeats `%2e%2e`). Match each
   component **case-insensitively against up-cased exFAT names** (exFAT is
   case-insensitive; matching case-sensitively would both miss real files and
   allow two paths to alias the same entry — GPT-5.5 #14). Reject components
   > 255 UTF-16 code units.
3. Resolve by **targeted descent** — walk only the named directories component by
   component, NOT a full `walk_volume`.
4. A torn/partially-written exFAT entry-set (the car creating a clip
   concurrently) → treat the component as not-yet-present → `NotFound`, never a
   half-decoded record.
5. Final component must be a **file** → its `FileRecord` + `ClipIdentity`.
   Missing / not-a-file → `NotFound` (no existence leak).

exFAT has no symlinks; with `..` rejected there is no in-volume escape. `webd`
additionally only issues reads for clip paths under `TeslaCam/` that the catalog
knows (§5), so arbitrary-path reads never reach the socket.

## 5. Consistency (lightweight, lun.0 only)

The car actively writes `teslacam.img`. Safety comes from three cheap rules — not
generation counters or per-chunk checksums:

- **Catalog-`stable` only.** `webd` issues a read only for a clip the indexer
  lists as `stable`; the `valid_data_length` clamp alone is not sufficient (it
  doesn't guard MP4-metadata inconsistency or cluster reuse).
- **Identity fence per HTTP request.** First chunk captures `ClipIdentity`
  (`first_cluster` + `total_size` + `name_hash`); `webd` echoes it on every
  later chunk. If the on-disk entry no longer matches → `Changed` → HTTP **410
  Gone**. The client never receives bytes stitched across two incarnations, and
  never a silent wrong-bytes-with-200.
- **Readable-window clamp.** `readable_size = valid_data_length`; reads return
  `eof` at that ceiling and never block the writer. For a still-growing clip the
  ceiling may rise between chunks; `Ok.readable_size` reports the current value.

Reads **never** mount and **never** eject `lun.0` — zero interaction with the #1
invariant. (`lun.1`/media never uses this seam — ADR-0003.)

## 6. Resource / DoS caps

- `MAX_READ_LEN = 8 MiB`; `offset`/`len` checked against `readable_size` with
  saturating math.
- Cap concurrent read connections (e.g. 4) — bounds in-flight memory and disk
  contention so car writes win.
- Per-connection read/write timeouts mirror the scan socket.
- Honor existing ionice/IOWeight so reads never starve the car.

## 7. `webd` side

- New `webd` client module (mirror the `gadgetd` client): connect to
  `scannerd-read.sock`, send `ReadFileRequest`, read the header + binary tail.
- Live-clip wiring in `media.rs`: when a clip is requested that is **not** in the
  archive, fall back to a slot-0 `ReadFile` loop (stable-only); on `Changed`
  return `410`. Archive copy is always preferred when present.
- **Media bytes do NOT use this client** — they are served by the RO `media.img`
  mount handler (separate task; see ADR-0003 / `docs/status.md`).

## 8. Acceptance criteria

- [ ] `ReadFile` returns byte-exact windows for a known TeslaCam fixture clip at
      various `offset`/`len`, including the final short window + `eof`.
- [ ] Path jail rejects `..`, absolute, backslash, NUL, over-long paths;
      case-insensitive match resolves a known clip regardless of case.
- [ ] `NotFound` for missing path / a directory; no existence leak.
- [ ] Mid-write clip: reads clamp at `valid_data_length`, never error, never
      block the writer (writer-simulating fixture).
- [ ] Identity change between chunks → `Changed` → HTTP 410 (never wrong bytes).
- [ ] Concurrent reads (≥ 4) succeed while an `indexd` scan runs on the other
      socket; the scan cadence is unaffected.
- [ ] Memory bounded (≤ `MAX_READ_LEN` per in-flight read) on a multi-GiB clip.
- [ ] No mount, no eject, no write — verified by the existing invariants tests.
