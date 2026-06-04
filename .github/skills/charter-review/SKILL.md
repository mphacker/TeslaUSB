---
name: charter-review
description: >
  Review code changes against the binding engineering standards for
  the TeslaUSB B-1 rewrite, which live in the spec set —
  `docs/specs/SPEC.md` §7 (code style & engineering standards),
  §8 (testing), §10 (boundaries) — plus the relevant component spec
  (`gadgetd.md`, `scannerd.md`, `indexd.md`, `webd.md`, `uploadd.md`,
  `retentiond.md`, `wifid.md`, `spa.md`, `storage.md`,
  `tesla-usb-contract.md`). Use when asked to charter-review, do a
  standards check, gate-review a spec deliverable or a migration step,
  do a pre-commit sweep, or review code for code smells / dead code /
  shortcut violations / architecture-layering violations on the B-1
  branch (`b1-userspace-rust`). Scope can be: a specific commit, a
  commit range, a working-tree diff, a PR number, a component-spec
  deliverable, a `migration.md` M-step, or specific files. Reviews
  Rust + the static SPA (TypeScript/JS) — there is NO Python in the
  B-1 runtime. Posts findings to the PR if PR-scoped; otherwise writes
  a structured report to the session log. Delegates security topics to
  a `security-review` skill if one is available.
---

# Standards / Charter Review (B-1)

Audit code changes against the **binding engineering standards of the
B-1 rewrite**. After the reset there is no separate charter document:
the standards ARE the spec set. The source of truth is, in priority
order:

1. **`docs/specs/SPEC.md` §7** — Code style & engineering standards.
2. **`docs/specs/SPEC.md` §10** — Boundaries (ALWAYS / ASK FIRST / NEVER).
3. **`docs/specs/SPEC.md` §8** — Testing strategy.
4. The **component spec** that owns the changed code (its "Boundaries"
   section is binding for that component).
5. This skill's **Five Pillars** review philosophy (below), which
   operationalises the operator's standing quality directive.

If anything in this skill contradicts the specs, **the specs win** (and
this skill should be updated). If the specs are *silent* on something
this skill flags, see "When to STOP and ASK".

The operator's standing quality directive (verbatim) that the Five
Pillars encode:

> *"We also want to make sure we don't have code smells, we follow
> best architecture practices, we don't take shortcuts or go with
> the 'easy' approach when there is a better approach that might
> just take a bit more work. Don't be lazy. Never leave bad code
> or bugs, fix things as they are found. No dead code."*

This is NOT a stylistic lint pass (CI / `clippy`/`fmt`/`eslint` do
that). It is a substantive review that finds smells, architecture
violations, shortcut patterns, dead code, and boundary breaches that
automation cannot detect.

**The non-negotiable backdrop — the #1 invariant.** The car must ALWAYS
be able to write TeslaCam. `gadgetd` is the only CRITICAL service and
the only code allowed near the car-facing write path
([`SPEC.md` §2, §10](../../../docs/specs/SPEC.md)). Any change that
could add latency or a failure mode to that path, or that lets a
non-`gadgetd` component touch the LUN, is a **Blocker** regardless of
code quality.

**Architecture the review assumes** (full Rust + static SPA — no
Python, no NBD):

| Crate / area | Role |
|---|---|
| `teslausb-core` | shared types, config, SQLite access, **SEI model** (pure parsing) — KEEP/extend |
| `gadgetd` | **CRITICAL**: kernel LUN + eject-handoff; sole owner of the write path |
| `scannerd` | raw exFAT/MP4/SEI reader; capped keyframe thumbnails |
| `indexd` | trips/events/clips derivation → SQLite; **sole SQLite writer** |
| `webd` | axum API + static SPA host |
| `uploadd` | cloud upload queue |
| `retentiond` | retention + archive + the space governor (`storage.md`) |
| `wifid` | STA/AP state machine + SDIO watchdog |
| `spa/` | static SPA (Preact/Svelte/Solid + vendored Leaflet + Chart.js) |
| `teslafat`, `teslausb-worker` | **LEGACY** — removed from the runtime; flag any *new* runtime use as a Blocker |

**When to use this skill (vs. others):**

| Skill | Scope | When |
|---|---|---|
| `charter-review` (this) | B-1 standards, architecture, code smells, dead code, boundaries | Every spec-deliverable gate, every B-1 PR, on demand |
| `security-review` (if available) | subprocess injection, path traversal, root usage, gadget/configfs safety, secrets | charter-review delegates security topics here; if absent, apply the inline checklist in Phase 6 |

---

## Phase 0 — Prerequisites

### Load the standards

Read, in full:

- `docs/specs/SPEC.md` (§7 standards, §8 testing, §9 prototype-first
  unknowns, §10 boundaries are the load-bearing sections).
- The **component spec** for whatever changed (e.g. a diff under the
  scanner → read `docs/specs/scannerd.md`).
- `docs/specs/README.md` for the spec index, and `docs/plan.md` for
  background architecture synthesis.

The specs are the source of truth. If this skill and a spec disagree,
the spec wins.

### Verify the branch

Standards review is only meaningful on the B-1 branch:

```bash
git branch --show-current
```

If the current branch is `main`, inform the user and ask whether they
want to switch — `main` predates the B-1 spec set and will produce
false positives.

### Confirm scope

Accept the scope from the invocation, or default to "working-tree diff
vs. base branch".

| Mode | Trigger examples | Behaviour |
|---|---|---|
| **commit** | "commit abc1234", "review the last commit" | One commit (`git show <sha>`) |
| **range** | "commits abc1234..def5678" | All commits in the range |
| **working-tree** | "what I have now", "before I commit", default | `git diff` + untracked files |
| **pr** | "PR #42", "#42" | Fetch via `gh pr diff` and review |
| **spec-deliverable** | "the `webd` deliverable", "the scanner work" | All files implementing a component spec; gate against that spec's Boundaries + SPEC §7–§10 (Phase 7) |
| **migration-step** | "M3", "the migration step" | The files/operations for an M-series step in `docs/specs/migration.md` (Phase 7) |
| **files** | "review `rust/crates/scannerd/src/reader.rs`" | Specific files |

---

## Phase 1 — Pre-flight automated gates

Run the automated checks the specs prescribe (`SPEC.md` §5 commands,
§8 testing). If these are red, **stop and report immediately** — fix
the automated failures before manual review.

### Rust gates

If any Rust files are in scope (from `rust/`):

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo deny check                 # licenses + advisories (rust/deny.toml)
```

Dead-code / docs (Pillar 5 support), run if the tools are installed:

```bash
cargo machete                    # unused dependencies
cargo +nightly udeps             # unused deps (alt), if nightly available
cargo doc --no-deps --document-private-items --workspace
```

Coverage (if `cargo-llvm-cov` is installed): focus on the **critical
paths** SPEC §8 calls out — the raw parser, the stability gating, the
eject-handoff state machine, the SEI decoder, and the `gadgetd`
invariant tests — not a blanket repo number.

Any non-zero exit on the four primary gates = **block**. Report which
gate failed with output.

### SPA gates

If any SPA files are in scope (`spa/`):

```bash
npm ci
npm run build                    # must emit a hashed static bundle
npm run test                     # component/unit tests
npx playwright test              # E2E + perf + console assertions
```

Plus, if configured in the SPA toolchain: `tsc --noEmit` (strict) and
the project's `eslint`. Any non-zero exit = **block**.

> **UI changes are not "done" on green unit tests.** Per
> `.github/copilot-instructions.md` and `SPEC.md` §8, every
> UI-affecting change is verified end-to-end in a real browser
> (perf, zero console/pageerror, screenshots at 375px + ≥1280px, proof
> the changed JS module is actually loaded). See Phase 6 UI/UX.

---

## Phase 2 — The Five Pillars (manual review)

For each changed file, walk the pillars in order. Cite the SPEC section
(or component spec) for every finding. Severity:

- **Blocker** — must fix before merge. Standards breach with no
  acceptable mitigation, or any #1-invariant / §10-NEVER breach.
- **Major** — should fix before merge. Standards breach with a
  documented mitigation.
- **Minor** — fix opportunistically.
- **Nit** — purely cosmetic. Optional.

### Pillar 1: No Code Smells (SPEC §7 — "no dead code, no speculative abstractions")

- [ ] **Function length** — any function over ~50 SLOC?
  `grep -nE '^\s*(pub\s+)?(async\s+)?fn ' <file>` (Rust) /
  `grep -nE '^\s*(export\s+)?(async\s+)?function ' <file>` (TS). **Major** if long without justification.
- [ ] **Nesting depth** — `if`/`for`/`match` deeper than 3 levels? **Major**.
- [ ] **Magic values** — semantic literals (`timeout = 30`, `max_retries = 5`) not named consts? **Major**. (Array indices, `0`/`1`, are fine.)
- [ ] **God modules** — file > 500 lines (target < 300)? **Blocker** > 500, **Major** 300–500. `wc -l <file>`.
- [ ] **Duplication** — does the diff re-add logic that already exists (often something that belongs in `teslausb-core`)? **Major** on third occurrence; **Minor** on second.
- [ ] **Primitive obsession** — `String`/`u64`/`PathBuf` crossing module boundaries where a newtype would carry meaning? **Minor** unless it crosses a boundary.
- [ ] **Data clumps** — 3+ params that always travel together but aren't a struct? **Major** if seen at 3+ call sites.
- [ ] **What-comments** — comments describing *what* the next line does rather than *why*. **Minor** each; **Major** if pervasive.
- [ ] **Cyclomatic complexity** — clippy flags Rust (`cognitive_complexity`). For TS, eyeball deeply-branched functions. **Major** for egregious cases.

### Pillar 2: Best Architecture Practices (SPEC §3, §6, §10)

- [ ] **Write-path isolation (#1 invariant)** — does any non-`gadgetd`
  code write to `disk.img`, the LUN, or mount the Tesla FS read-write
  while the car owns it? → **Blocker**. Pi-side writes happen via the
  eject-handoff in `gadgetd` only.
- [ ] **Single-writer discipline** — does anything other than `indexd`
  write to `index.sqlite3`? → **Blocker** (indexd is the sole SQLite
  writer; [`indexd.md`](../../../docs/specs/indexd.md)).
- [ ] **Domain purity** — the SEI/parse model in
  `teslausb-core/src/sei` (and other pure parsing/model code) must stay
  I/O-free: no `tokio::net`, `std::process`, `tokio::fs`, sockets, or
  SQLite calls inside the parser. Service binaries own process /
  network / filesystem orchestration. → **Blocker** for I/O in a pure
  model module.
- [ ] **No legacy runtime** — any *new* runtime dependency on
  `teslafat` (NBD synthesizer) or `teslausb-worker`, or any
  reintroduction of NBD / Python-Flask? → **Blocker**
  ([`SPEC.md` §10 NEVER](../../../docs/specs/SPEC.md)).
- [ ] **Dependency inversion** — new code that constructs its own
  infrastructure deps instead of accepting them via parameters? **Major**.
- [ ] **SRP** — a new `utils.rs` / `helpers.ts` is a red flag (`utils`
  is not a responsibility). **Major** for new catch-all modules;
  **Blocker** if a module mixes 3+ unrelated concerns.
- [ ] **Pure functions where possible** — new code mixing I/O with
  logic where a pure function + thin adapter would be clearer? **Major**.

### Pillar 3: No Shortcuts (SPEC §7)

| Shortcut | Required alternative | Severity |
|---|---|---|
| `unwrap()` / `expect()` in a service path that could realistically fail | `?` propagation / explicit handling, return `Result` | **Blocker** (Rust) |
| `panic!`/`assert!` for a runtime (non-invariant) error | `Result` return | **Blocker** |
| `unsafe` block without a `// SAFETY:` comment **and** a test | justify every invariant; add the test | **Blocker** |
| `println!`/`eprintln!`/`print!`/`dbg!` | `tracing` | **Blocker** |
| `console.log`/`debugger` shipped in the SPA bundle | remove / use a gated logger | **Blocker** |
| `TODO`/`FIXME` without a linked issue | `// TODO(#123): …` | **Major** |
| Commented-out code | delete it (git remembers) | **Blocker** |
| New public function with no test (non-trivial) | add the test now | **Major** |
| Blocking I/O on the tokio runtime (`std::fs`, blocking SQLite/lock across an `await`) | async equivalents / `spawn_blocking` | **Blocker** |
| `time::sleep` used as a synchronisation primitive | channels / notifications | **Major** |
| Loading a whole video/clip into RAM | streaming, bounded buffers | **Blocker** (memory discipline, SPEC §7) |
| Magic retry counts / timeouts buried in code | named const or config | **Major** |
| Boolean parameter that switches behaviour | enum variants | **Major** |

### Pillar 4: Fix Bugs Immediately (SPEC §7 directive)

- [ ] **Bug fix without a regression test** — every fix MUST add a test
  that fails before and passes after. **Blocker**.
- [ ] **Boy-Scout rule** — adjacent smells in a touched file either
  fixed or filed. **Minor** for not-fixed; **Major** for not-filed.
- [ ] **TODO sweep** — new unlinked `TODO`? **Major**.

### Pillar 5: No Dead Code (SPEC §7, §10)

- [ ] **Unused imports / deps** — Rust: `cargo machete` + clippy
  `unused_imports`. TS: `eslint` no-unused-vars. **Blocker** (CI
  should catch; verify).
- [ ] **Unused functions / unreachable code** — `cargo udeps` /
  `#[allow(dead_code)]` without justification. **Major**; **Blocker**
  if introduced in this change.
- [ ] **Unused parameters** — `#[allow(unused)]` without an ADR-style
  justification. **Major**.
- [ ] **Commented-out code** —
  `grep -nE '^\s*(//|/\*).*(let |fn |use |function |const |import )' <file>`. **Blocker** — delete it.
- [ ] **Empty / placeholder modules** outside known scaffolding. **Major**.
- [ ] **Vestigial config** — a new config key no code reads, or a
  removed key still in the example config. **Major** if added.
- [ ] **"Backup"/"old" files** — `*.bak`, `*_old.*`, `*_v2.*`,
  `*_new.*`. **Blocker** — must not be committed.

---

## Phase 3 — Language-specific rules

### Rust deep-dive (SPEC §7 "Rust" + "Memory discipline")

For every Rust file in the diff:

- [ ] **Lints enforced** — the crate inherits the workspace lints that
  make `clippy -D warnings`, no-`unwrap`/`expect` in service paths,
  and `unsafe` discipline real. Flag a crate that opts out of the
  workspace lints without justification. **Blocker**.
- [ ] **No `unwrap()`/`expect()` in service paths** — return `Result`
  and handle errors. **Blocker** (test code may use them).
- [ ] **`unsafe` only at the kernel/FFI boundary** — with a `// SAFETY:`
  comment **and** a test exercising the invariant. **Blocker** otherwise.
- [ ] **`tracing`, not `println!`/`eprintln!`/`dbg!`**. **Blocker**.
- [ ] **No `static mut` / mutable `lazy_static!`** — use `OnceLock`
  for immutable init, `Mutex`/`RwLock` for mutable, or inject state.
- [ ] **Memory discipline (512 MB Pi)** — bounded buffers, streaming
  I/O, no whole-video-in-RAM. Non-critical services must run under a
  cgroup `MemoryMax`; `gadgetd` gets `OOMScoreAdjust=-1000`. If the
  change adds a systemd unit or buffer, verify it respects the OOM
  kill order `uploadd → wifid → webd → scannerd → retentiond → indexd
  → NEVER gadgetd`. **Major** (or **Blocker** if it risks `gadgetd`).
- [ ] **Async correctness** — no blocking I/O on the runtime (Pillar 3).
- [ ] **Typed errors in libraries** — library/shared code prefers
  typed errors; only a binary's outer layer uses a catch-all error.
  **Major** for a catch-all (`Box<dyn Error>`) at a public library
  boundary.
- [ ] **Public API docs** — `///` on every `pub` item. **Minor** for
  self-evident one-liners; **Major** otherwise.

### SPA deep-dive (SPEC §7 "SPA" + §8 UI)

For every SPA file in the diff:

- [ ] **No heavy framework** — the SPA is a *small* framework
  (Preact/Svelte/Solid). Adding React+Redux or another heavyweight
  stack is a **Blocker** (SPEC §7; ASK FIRST per §10).
- [ ] **Vendored parity libs** — map/charts are **Leaflet +
  MarkerCluster** and **Chart.js**; the HUD uses the existing
  `dashcam-mp4` SEI approach. Swapping to **MapLibre** (explicitly
  rejected) or another lib that changes look/feel is a **Blocker**.
- [ ] **Hashed static bundle** — the build emits a hashed bundle served
  by `webd`. Flag un-hashed/un-fingerprinted asset wiring. **Major**.
- [ ] **No secrets in the bundle** — no OAuth tokens, WiFi/Samba creds,
  or API keys baked into client code or shipped to the Tesla volume.
  **Blocker** ([`SPEC.md` §7 security](../../../docs/specs/SPEC.md)).
- [ ] **Client-side HUD only / no transcoding** — telemetry HUD is
  rendered client-side over native `<video>`; the Pi never transcodes.
  Flag any server-side frame work. **Blocker**.
- [ ] **TypeScript strictness** — if TS, no implicit `any`; strict mode
  on. **Major** for new implicit `any`.
- [ ] **No shipped `console.log`/`debugger`**. **Blocker** (Pillar 3).
- [ ] **Look/feel + feature parity preserved** — the rewrite must keep
  the existing UX ([`spa.md`](../../../docs/specs/spa.md)). A dropped
  or materially redesigned screen is **ASK FIRST** (SPEC §10), not a
  silent change.

---

## Phase 4 — Boundary compliance (SPEC §10)

Turn `SPEC.md` §10 into review checks. For every change:

- [ ] **#1 invariant supreme** — nothing adds latency/failure to the
  car's write path; only `gadgetd` is CRITICAL; everything else is
  memory-capped. **Blocker** on breach.
- [ ] **Eject-handoff for Pi-side writes** — never mount the Tesla FS
  RW while the car owns it; never mutate during an active save.
  **Blocker**.
- [ ] **Parse-once / render-client-side / never transcode**. **Blocker**.
- [ ] **Derived state stays on Pi-side ext4** — SQLite/WAL and derived
  state live outside `disk.img`, never on the Tesla volume. **Blocker**.
- [ ] **Deploy/migrate only via the hardware-test skill** — reversibly,
  backups first, SSH/WiFi/boot protected. Flag any code/script that
  hand-deploys to the device. **Major**.
- [ ] **NEVER list** — dm-thin/CoW under the LUN; an unbounded block
  snapshot under the live LUN; a non-`gadgetd` service rebooting the
  Pi or restarting the gadget; concurrent RW mount of the Tesla FS;
  Python/Flask or NBD/`teslafat` back in the runtime/write path;
  committed secrets. Any hit = **Blocker**.
- [ ] **ASK-FIRST triggers** — write-path latency/failure risk;
  reflash/repartition (S2); dropping/redesigning a user-facing
  feature; a new heavyweight dependency/language/toolchain; any
  irreversible live-device op. If the change does one of these without
  the operator having explicitly approved it, **flag for confirmation**
  rather than approving.
- [ ] **Hardware-dependent claims are spike-backed** — if the change
  *depends on* an unproven hardware behavior (a §9 unknown — LUN
  acceptance, eject/rebind, boot time, parse stability, WiFi TX cap,
  microSD contention, disk.img sizing), the gating spike must have
  PASSed per
  [`hardware-first-development.md`](../../../docs/specs/hardware-first-development.md).
  Building on an unproven assumption = **Blocker** (it is the exact
  failure mode that doc exists to prevent).

---

## Phase 5 — Anti-pattern sweep

For each, do a `grep` constrained to the diff context and flag hits:

- [ ] **"Just suppress the warning"** — new `#[allow(...)]` /
  `// eslint-disable` / `@ts-ignore` without a comment explaining why.
  **Blocker** without comment.
- [ ] **"It's just a quick fix"** — a catch-all error swallow
  (`Box<dyn Error>` / `catch {}`) at a module boundary. **Blocker**.
- [ ] **Stringly-typed code** — new `HashMap<String, String>` (or TS
  `Record<string, string>`) where a struct/interface would carry the
  schema. **Major**.
- [ ] **Boolean trap** — a `bool` parameter that switches behaviour.
  Use enum variants. **Major**.
- [ ] **Catch-all retry** — a generic `retry(times=N)` that hides
  recoverable vs. non-recoverable errors. **Major**.
- [ ] **Magic timeout** — a timeout literal not from config or a named
  const. **Major**.
- [ ] **Mega-function** — one function doing parse + validate + compute
  + I/O + format + log. Split it. **Major**.
- [ ] **Comment-as-bug-deferral** — `// FIXME: wrong but works most of
  the time`. Fix now or file an issue; never ship a known-wrong
  behaviour. **Blocker**.

---

## Phase 6 — Delegated reviews

### Security

If the diff touches any of:

- Subprocess invocation (`std::process::Command`).
- File path construction from request/user input.
- configfs writes / gadget LUN management (`gadgetd`).
- The axum listener / network binding (`webd`), or `wifid` AP/STA
  control, or the Samba on/off toggle.
- Any code running as root or via `sudo`.
- Secrets: cloud **OAuth refresh tokens**, rclone config, WiFi/Samba
  credentials (must be root-only `0600`, never logged, never in the
  bundle or on the Tesla volume — SPEC §7 trust model).
- Media install (chime/lightshow/boombox/music upload to p2).
- The cleanup / retention / space-governor path (anything that deletes
  files — `retentiond`, [`storage.md`](../../../docs/specs/storage.md)).

→ **If a `security-review` skill is available, invoke it** in
changed-mode against the same scope. If it is **not** available, apply
the trust-model checklist inline (SPEC §7 +
[`webd.md` security](../../../docs/specs/webd.md)) and record findings
here. Charter-review does not duplicate security work; it ensures the
security topics are reviewed.

### UI/UX

If the diff touches the SPA (`spa/`) or anything `webd` serves to the
browser:

→ Apply the UI verification rules in `.github/copilot-instructions.md`
and [`spa.md`](../../../docs/specs/spa.md). Confirm the change was
verified **end-to-end with Playwright**:

- Real browser drive (not just a 200 from the endpoint).
- Perf captured: navigation TTFB, DOMContentLoaded, FCP, slowest 5–10
  requests.
- **Zero** console / pageerror.
- Screenshots at **375px** and **≥1280px**.
- Proof the changed JS module is actually loaded by the served page.
- Look/feel + feature parity preserved.

→ **Blocker** if a UI-affecting change is declared done without this
verification; **Major** for partial verification.

---

## Phase 7 — Deliverable / migration gate

### `scope = spec-deliverable`

Verify the component spec's own **"Boundaries"** section is satisfied,
its required tests exist (SPEC §8 — e.g. property/fixture tests for the
parser/stability/SEI; `gadgetd` invariant tests proving a
crash/handoff looks like a clean unplug), and there are **no** SPEC §10
NEVER violations. If the deliverable depends on a §9 hardware unknown,
its gating spike must have PASSed
([`hardware-first-development.md`](../../../docs/specs/hardware-first-development.md)).

### `scope = migration-step`

Verify the M-step's "verify after" checks in
[`migration.md`](../../../docs/specs/migration.md), that the step is
reversible with backups taken first, and that it was (or will be) run
**only** through the hardware-test skill.

**If the deliverable's / step's acceptance criteria are not met, the
review concludes `BLOCKED — deliverable incomplete`, even if individual
file reviews are clean.**

---

## Phase 8 — Report

Output a structured report. If `scope = pr`, post via
`gh pr review --comment` (multi-paragraph body). Otherwise write to
`~/.copilot/session-state/<session-id>/files/charter-review-<timestamp>.md`
and surface the path.

### Report template

```markdown
# Standards Review — <scope description>

**Scope:** <commit / range / pr / spec-deliverable / migration-step / files>
**Reviewer:** charter-review skill (B-1)
**Date:** <ISO date>
**Standards:** docs/specs/SPEC.md §7–§10 @ <git sha> (+ <component spec>)

## Summary

- **Blockers:** N
- **Majors:** N
- **Minors:** N
- **Nits:** N
- **Automated gates:** PASS / FAIL (fmt / clippy / test / deny / SPA build / Playwright)

**Verdict:** APPROVED / APPROVED WITH NITS / CHANGES REQUESTED / BLOCKED

## Automated gate results

| Gate | Status | Notes |
|---|---|---|
| `cargo fmt --all --check` | ✅ | |
| `cargo clippy -D warnings` | ❌ | 3 warnings, see findings |
| `cargo test --workspace` | ✅ | |
| `cargo deny check` | ✅ | |
| `npm run build` / `playwright test` | ✅ | |

## Findings

For each, in priority order (Blocker → Major → Minor → Nit):

### [BLOCKER] <one-line title>
**File:** `path/to/file.rs:LINE`
**Standard:** SPEC §<n> / <component spec> / Pillar <m> — "<rule>"
**Issue:** <what's wrong>
**Why it matters:** <how it breaches the standard / what could go wrong>
**Required action:** <specific fix>

---

(repeat for each finding)

## Delegated reviews

- Security: invoked? (skill / inline) scope? findings?
- UI/UX: Playwright-verified? findings?

## Deliverable / migration gate (if applicable)

- [x] Criterion 1 — verified
- [ ] Criterion 2 — NOT MET, see Blocker #N

## Recommended next actions (in order)

1. ...
2. ...
```

---

## When to STOP and ASK

This skill enforces the specs; it does NOT override them. If the diff
contains something that looks flaggable but the specs are **silent**:

1. Note it in the report under "Spec-silent observations" — neutrally.
2. Surface it to the user:
   "The specs don't currently cover X. The diff does Y. Should
   `SPEC.md` §7 be amended?"
3. If the user wants the standard added, ALSO open a PR to
   `docs/specs/SPEC.md` (or the relevant component spec) adding the
   rule.

**Never silently rubber-stamp** something the specs forbid because
"it's a small case." The specs are binding; exceptions are explicit.

**Never block** something the specs don't forbid on reviewer intuition
alone. Add a spec-silent observation and raise it; the spec wins until
amended.

---

## Performance note

A full review on a multi-file PR can take 10–20 minutes of analysis.
For working-tree diffs during active development, the user may invoke
`charter-review --fast`, which:

- Skips automated gates (assumes the dev runs them locally).
- Reads only the diff hunks, not full files.
- Reports only Blockers + Majors.

`--fast` is for pre-commit sanity. Real PR review always runs full.
