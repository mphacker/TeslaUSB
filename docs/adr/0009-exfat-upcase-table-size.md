# ADR-0009 — Ship a 256-byte ASCII-only exFAT upcase table

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-19 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 3 preflight of B-1 rewrite |
| Trigger  | Hardware finding D3 from Phase H2.6 (exFAT read-only smoke test) |

## Context

Phase H2.6 ran `fsck.exfat -v /dev/nbd0` against a synthesized exFAT
volume served by `teslafat-test@0.service` over NBD on
`cybertruckusb.local`. The Linux kernel exfat driver mounted the
volume cleanly, read every file byte-identical to the backing tree,
and produced no errors. But `fsck.exfat` (from `exfatprogs` 1.2.9 as
shipped on Debian/Raspberry Pi OS Bookworm) reported:

```
upcase table is invalid, use default
ERROR: corrupted upcase table 0 (expected: 0x6c72721c)
```

This was filed as defect **D3** in `docs/01-PROGRESS.md`. D3 was
classified HIGH because Phase 3 (write-path) cannot ship until the
volume is clean to every common consumer — Tesla, Windows, and macOS
all run `fsck`-equivalent checks at mount time, and a "corrupted
upcase table" report would prevent the gadget from being trusted as
storage.

### Root cause analysis

Full bit-by-bit reproduction in a local Rust integration test
(`teslafat::tests::d3_upcase_debug`, since deleted) showed:

1. Our `UpcaseTable::ascii_identity()` produced byte-identical
   output between the dev box and the Pi.
2. The stored `TableChecksum` field in the upcase directory entry
   (`0x6c72721c`) matched the spec §7.2.5.3 algorithm applied to
   our table bytes.
3. The same checksum value appeared in **both** sides of the
   `fsck.exfat` error message — the "expected" value was actually
   our (correct) stored value, not an `fsck`-computed expectation.

Inspection of `exfatprogs` 1.2.9 source
(`lib/libexfat.c::boot_calc_checksum()` and
`fsck/fsck.c::read_upcase_table()`) showed the root cause:

```c
void boot_calc_checksum(unsigned char *sector, unsigned short size, ...
                                                ^^^^^^^^^^^^^^
```

The `size` parameter is declared `unsigned short` (16-bit). When
`read_upcase_table()` passes our 131,072-byte table size, the value
truncates to `131072 & 0xFFFF = 0`. The checksum loop runs zero
iterations, so `fsck`'s computed checksum stays `0`. The format
string

```c
"ERROR: corrupted upcase table %#x (expected: %#x)",
checksum,                /* fsck's truncated-to-zero computation */
dentry->upcase_checksum  /* our correct stored value */
```

then renders as `"corrupted upcase table 0 (expected: 0x6c72721c)"`
— "expected" is a misleading label; both numbers are different
takes on the same field.

The bug is **still present** on `exfatprogs`'s `master` branch as of
2026-05-19; no upstream fix is in flight. We cannot wait on a Debian
package update.

### Why the kernel exfat driver did not flag it

The Linux exfat driver loads the upcase table at mount time but
**does not** validate the `TableChecksum` field — it just uses the
served bytes for filename comparisons. That's why H2.6 saw clean
mount + clean reads but dirty `fsck`.

Windows and macOS may behave differently. We have not confirmed
their behaviour because we lack a way to mount a B-1-synthesized
volume on either OS in this session (D3 was found mid-deploy and
the operator's machine is Windows-only running the dev tooling).
The safe assumption is that any Microsoft-implemented driver
checks the checksum — Microsoft authored the spec, after all —
and would reject our volume the same way `fsck` did.

### Design space for a fix

The spec (§7.2.4 "Up-case Table") explicitly allows partial tables:

> *"If the table size is less than 0x10000 (the maximum), the
> characters with indices greater than (or equal to) the table size
> MUST map to themselves."*

Both Linux's exfat driver and `mkfs.exfat` already use this
allowance — `mkfs.exfat` ships Microsoft's canonical compressed
table at ~5,836 bytes (well under 65,536), not the full
131,072-byte BMP table.

So three options exist:

**Option A — Ship the full 131,072-byte BMP table.** Status quo
before this ADR. Pro: covers every BMP code unit explicitly. Con:
loses to the exfatprogs u16 truncation bug on every fsck. Rejected.

**Option B — Ship Microsoft's canonical compressed table
(~5,836 bytes).** Pro: maximum compatibility, covers Latin-1, Greek,
Cyrillic, etc. Pro: same table `mkfs.exfat` ships, so cross-tool
diffing is trivially clean. Con: requires implementing the
compressed-encoding format (spec §7.2.5.4) including `0xFFFF`
skip markers; ~200 LOC + cross-validation against the canonical
reference. Con: only marginal value-add for the Tesla camera
use-case (filenames are ASCII timestamps; non-ASCII folding is
never exercised). Deferred to a future ADR if non-ASCII filenames
ever become a requirement.

**Option C — Ship a 256-byte ASCII-only uncompressed table.** Pro:
trivial implementation (128 little-endian `u16` entries:
`0x0061..=0x007A` fold to `0x0041..=0x005A`, all others identity).
Pro: 256× safety margin under the exfatprogs `u16` ceiling
(65,536 / 256 = 256). Pro: zero non-ASCII coverage loss for
TeslaCam — filenames like `2026-01-15_14-32-15-front.mp4` are
pure ASCII. Pro: uncompressed format requires no skip-marker
implementation. Con: a non-Tesla user who renames a copied clip
with a non-ASCII character on Windows would find that case-folding
no longer happens for that character (the driver would identity-map
it). Acceptable — files are still preserved; only case-insensitive
filename search would be affected.

## Decision

§A — **Ship a 256-byte ASCII-only uncompressed upcase table** as
the canonical exFAT upcase table for the B-1 rewrite. Implementation
lives in `rust/crates/teslausb-core/src/fs/exfat/upcase_table.rs`
under `UpcaseTable::ascii_identity()`. Symbolic constants:

- `UPCASE_TABLE_ENTRIES: u16 = 128`
- `BYTES_PER_ENTRY: usize = 2`
- `UPCASE_TABLE_SIZE_BYTES: usize = 256`
- `MAX_INTEROP_UPCASE_TABLE_SIZE_BYTES: usize = 0xFFFF`

§B — **All exFAT upcase table changes are bounded by
`MAX_INTEROP_UPCASE_TABLE_SIZE_BYTES = 0xFFFF`** until an upstream
fix lands in the Debian-shipped `exfatprogs`. The bound is enforced
at compile time (const-assert) and pinned at test time
(`table_size_is_below_exfatprogs_1_2_9_u16_truncation_limit`). Any
PR that grows the table past this ceiling MUST first write a
superseding ADR documenting:

1. Which Debian release ships the patched `exfatprogs`.
2. Which Pi OS image (or apt source pin) makes the patched binary
   available on the target hardware.
3. A new H-series hardware test plan that verifies a >65,536-byte
   table no longer trips the bug.

§C — **The `UpcaseTable::uppercase()` API returns identity for
code units at or above the table size**, per spec §7.2.4. Callers
do not need any change — the API is unchanged from the
131,072-byte era for ASCII code units, and for non-ASCII code
units the new behaviour matches what the old code returned for
non-fold characters anyway (the BMP table was ASCII-fold-plus-
identity; the new table is ASCII-fold + implicit-identity).

§D — **The checksum value `0x88E38EE3` is pinned by test**
(`checksum_matches_pinned_ascii_table_value`). Any change to the
table bytes during Phase 3+ refactors must update the pinned value
in the same commit; the regression test will catch silent drift.

§E — **No fallback to the full BMP table for "compatibility"
mode**. Either the 256-byte table works on the target consumer
(Tesla / Windows / macOS) or we move to Option B (canonical
compressed). The 131,072-byte path is removed and not retained as
dead code.

## Consequences

**Positive:**

- `fsck.exfat -v` reports `clean` on hardware (verified post-fix
  in H2.6 re-run — see `hw-results.md`).
- Table generation cost drops from "131,072 byte allocation" to
  "256 byte allocation" — negligible already but now a clear
  non-issue on the Pi Zero 2 W.
- The interop ceiling is documented in code, so a future
  contributor who naively grows the table will hit the const-assert
  before shipping.
- No drift between dev box, Pi, and Tesla — all three see the
  same 256-byte payload.

**Negative:**

- Non-ASCII case folding is lost for the synthesized volume's
  filename comparisons. This is fine for TeslaCam (no non-ASCII
  filenames) but limits the volume's usability as a general-purpose
  exFAT mount where a user might `cp Tëst.mp4 .`. The volume would
  preserve the file byte-perfectly; only case-insensitive search on
  the `ë` would lose the folding semantics. Acceptable per scope
  of B-1 (TeslaCam-only target).
- We have ratified a workaround for a third-party bug in our own
  on-disk schema. Future maintainers MUST read this ADR before
  changing the table size; the consequence of forgetting is a
  hardware regression that is invisible on Linux but visible on
  Windows / macOS / Tesla.

**Trade-offs accepted:**

- Microsoft's canonical compressed table is not implemented. If a
  future requirement surfaces (e.g., supporting non-ASCII filenames
  in a backing tree), Option B becomes attractive; the API is
  unchanged so the implementation swap is contained inside
  `upcase_table.rs`.
- We do not attempt to detect a patched `exfatprogs` at runtime
  (impossible from inside the gadget anyway). The decision is
  static: 256 bytes always.

## Alternatives considered

**A. Wait for upstream fix.** Rejected. Bug is on `master`, no PR
in flight, no commitment from maintainers, Debian package update
cycles measure in years. Production blocker.

**B. Patch `exfatprogs` in-tree.** Rejected. We do not own that
codebase; carrying a patch increases maintenance burden across
every Pi OS upgrade and only helps users who run our patched
binary.

**C. Ship Microsoft's compressed table (~5,836 bytes).** Deferred
to a future ADR. Higher implementation + verification cost than
the 256-byte option; benefit (Latin-1, Greek, Cyrillic folding)
is unused by the target use-case.

**D. Detect the bug at runtime and produce a smaller table only
on affected systems.** Rejected. The Pi cannot introspect the
consumer's exfatprogs version, and the bug is in the consumer not
the producer. The producer (us) has no way to negotiate.

## References

- D3 root cause analysis: session summary 2026-05-19 (see
  `~/.copilot/session-state/.../files/charter-review-d3-fix.md`
  for the full charter review and pinned-checksum reasoning).
- exFAT spec v1.00 §7.2 Up-case Table Directory Entry, §7.2.4
  Up-case Table, §7.2.5 Up-case Table data, §7.2.5.3 Up-case
  Table checksum algorithm.
- `exfatprogs` source: `lib/libexfat.c::boot_calc_checksum()` and
  `fsck/fsck.c::read_upcase_table()` (commits as of 1.2.9 and
  master @ 2026-05-19).
- Implementation:
  `rust/crates/teslausb-core/src/fs/exfat/upcase_table.rs`.
- Const-asserted interop ceiling:
  `MAX_INTEROP_UPCASE_TABLE_SIZE_BYTES` at the same path.
- Regression tests: `table_size_is_below_exfatprogs_1_2_9_u16_truncation_limit`,
  `checksum_matches_pinned_ascii_table_value`, and the existing
  `checksum_matches_independent_reference` (which corroborates
  the pinned value by recomputing the spec algorithm).
- Phase H2.6 hardware finding: `docs/01-PROGRESS.md` D3 row.
- Phase H2.6 re-run results: `hw-results.md` (post-fix
  hardware validation).
