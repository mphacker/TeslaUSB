# ADR-0012 — `rustix` for `statvfs(3)` in the cleanup worker

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-20 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 4b.3 (cleanup) |
| Triggers | Charter §"ADR discipline" — new third-party dep; introduces (avoids) `unsafe` |

## Context

Phase 4b.3's cleanup worker needs to read the free-space
percentage on the backing volume so it can switch to "pressure"
mode (deleting eligible clips before they age out) when the
volume falls below `min_free_pct`. The POSIX call for this is
`statvfs(3)`, which `std` does not expose.

The cleanup worker runs as a non-root systemd service on the Pi,
reads one numeric value per cleanup tick (default every 300 s),
and is the only first-party caller of any POSIX file-system
metadata API in the workspace.

Constraints from the charter:

* `unsafe_code = "deny"` at workspace level. Any `unsafe` block
  is an exception that must be justified, and must carry a
  `// SAFETY:` comment.
* `clippy::all = deny` + `pedantic = warn`. Manual `MaybeUninit`
  dances tend to trip `cast_possible_truncation`,
  `cast_lossless`, and friends in pedantic mode.
* "Pick the Hard Right" framework — choose the better approach
  even when it's more work, where "better" includes "fewer
  invariants for the next maintainer to verify."
* ADR required for every new third-party dep.

## Options considered

### Option A — direct `libc::statvfs` + `unsafe`

* **Pros:** Zero new transitive deps (libc is already pulled in
  via rusqlite/inotify).
* **Cons:**
  * Introduces the first `unsafe` block in the entire workspace.
    Requires both a `// SAFETY:` comment AND a `#[allow(unsafe_code)]`
    override against the workspace deny-lint.
  * Manual `MaybeUninit::<libc::statvfs>::zeroed()` → call →
    `assume_init()` dance. Easy to get wrong; easy for a future
    maintainer to subtly break.
  * Manual `CString::new` from `OsStr` for the path, with explicit
    NUL-byte error handling.
  * `f_blocks` / `f_frsize` field widths vary across libc targets
    (`u64` on glibc x86_64, `u32` on musl, etc.); pedantic clippy
    flags the casts.
  * Three separate sources of footgun (FFI, init-discipline,
    path-encoding) for *one numeric read*.

### Option B — `rustix::fs::statvfs` (chosen)

* **Pros:**
  * Returns a safe `StatVfs` struct; no `unsafe` in our code at
    all. The workspace stays unsafe-free.
  * No `MaybeUninit`. No `CString` dance — `rustix` accepts
    `impl rustix::path::Arg`, which `&Path` satisfies natively.
  * `rustix` is the standard-issue modern POSIX wrapper crate.
    Pulled in by `cargo`, `ripgrep`, `uutils-coreutils`,
    `tokio` (via `mio` on some platforms), and many others —
    extremely well-audited.
  * MIT/Apache-2.0; no GPL contamination concerns.
  * Field widths normalised by rustix; one cast path covers all
    libc targets.
* **Cons:**
  * One new direct dep (compile-time only — `rustix` produces no
    runtime `.so` linkage; it inlines syscalls).
  * Slightly larger compile graph (acceptable: `cargo build`
    delta < 1 s on dev box).

### Option C — `nix` crate

* **Pros:** Also wraps `statvfs` safely.
* **Cons:** Heavier surface area than rustix (more sub-modules
  pulled in by default). Less actively maintained for the
  no-`unsafe`-in-API guarantees rustix targets.

### Option D — invoke `df -P` via `std::process::Command`

* **Pros:** Zero new deps.
* **Cons:** Subprocess for one numeric read; parse text output;
  fails the security skill's "subprocess invocation" trigger
  for no reason. Strictly worse.

## Decision

**Adopt rustix 0.38 with the `fs` feature, target-gated to
Linux** (matching `inotify`). The cleanup worker calls
`rustix::fs::statvfs(path)` and uses the returned `StatVfs`'s
`f_blocks`/`f_bavail`/`f_frsize` fields directly.

Non-Linux hosts continue to compile a stub that returns
`100.0` so the dev workstation test suite never sees synthetic
pressure.

This decision overrides Option A's smaller dependency footprint
in favour of the charter's "Pick the Hard Right" directive — the
"harder right" here is the option that *eliminates an entire
class of footgun* (`unsafe` + FFI + manual init).

## Consequences

* Worker `Cargo.toml` gains `rustix = { version = "0.38", features = ["fs"] }`
  under the Linux target block. The previously-added `libc`
  direct dep is removed (it remains transitively present via
  rusqlite/inotify, which is correct).
* The cleanup module has zero `unsafe` blocks and zero
  `#[allow(unsafe_code)]` overrides.
* Future POSIX needs (e.g., `fstatat`, `renameat2`, `O_DIRECT`
  open flags) have an established crate to reach for. We will
  not need to revisit this for each new POSIX call.
* If rustix is ever yanked (extremely unlikely — it is part of
  the unofficial Rust stdlib ecosystem), the swap-back to libc
  is mechanical and confined to one file.

## References

* Cargo.toml workspace lint config (`rust/Cargo.toml`).
* Charter §"Rust standards", §"Pick the Hard Right" framework.
* Sister ADR-0011 (inotify) — same target-gating pattern.
* cleanup module: `rust/crates/teslausb-worker/src/cleanup.rs`.
