---
name: charter-review
description: >
  Review code changes against the TeslaUSB B-1 Code Quality Charter
  (`docs/03-CODE-QUALITY-CHARTER.md`). Use when asked to charter-review,
  do a charter check, audit charter compliance, gate-review a phase,
  do a pre-commit charter sweep, or review code for code smells / dead
  code / shortcut violations / architecture-layering violations on the
  B-1 branch (`b1-userspace-rust`). Scope can be: a specific commit, a
  range of commits, a working-tree diff, a PR number, an entire phase
  deliverable, or specific files. Posts findings to PR if PR-scoped;
  otherwise writes a structured report to the session log. Distinct from
  `review-pr` (which targets v1's Flask architecture) and from
  `security-review` (which is a separate pillar).
---

# Code Quality Charter Review

Audit code changes against `docs/03-CODE-QUALITY-CHARTER.md` — the
binding standards for the B-1 rewrite. The charter encodes the
operator's directive (2026-05-19, verbatim):

> *"We also want to make sure we don't have code smells, we follow
> best architecture practices, we don't take shortcuts or go with
> the 'easy' approach when there is a better approach that might
> just take a bit more work. Don't be lazy. Never leave bad code
> or bugs, fix things as they are found. No dead code."*

This skill operationalises that directive at review time. It is
NOT a stylistic lint pass (CI does that). It is a substantive review
that finds smells, architecture violations, shortcut patterns, dead
code, and other charter breaches that automation cannot detect.

**When to use this skill (vs. other review skills):**

| Skill | Scope | When to invoke |
|---|---|---|
| `charter-review` (this) | B-1 charter compliance, architecture, code smells, dead code | Every phase gate, every B-1 PR, on demand |
| `review-pr` | v1 PR conventions (Flask, mount safety, IMG gating) | v1 PRs only — NOT applicable to B-1 |
| `security-review` | Subprocess injection, path traversal, root usage, gadget safety | Both v1 and B-1; charter-review delegates here for security topics |

**Charter sections this skill enforces:**

1. The Five Pillars (no code smells, best architecture, no shortcuts,
   fix bugs immediately, no dead code)
2. Rust standards (`unsafe_code = "deny"`, `unwrap_used = "deny"`,
   etc., plus `thiserror` for libs / `anyhow` for binary outer layer,
   `tracing` not `println!`)
3. Python standards (ruff rule families, `mypy --strict`, no `Any`,
   no `print()`)
4. Architectural Principles — Layering Rule, Dependencies inversion,
   ADR discipline
5. Anti-patterns rejected with concrete examples (see charter)
6. "Pick the Hard Right" decision framework

---

## Phase 0 — Prerequisites

### Load the charter

Read `docs/03-CODE-QUALITY-CHARTER.md` in full. The charter is the
source of truth. If anything in this skill contradicts the charter,
the charter wins (and this skill should be updated). Read also
`docs/00-PLAN.md` "Non-negotiable invariants" and "Decisions" tables —
those carry charter-adjacent rules.

### Verify the branch

Charter-review is only meaningful on the B-1 branch:

```bash
git branch --show-current
```

If the current branch is `main` or any v1 branch (`v2/*`, etc.),
inform the user and ask whether they want to switch — charter-review
on v1 code will produce massive false-positive output because v1
predates the charter.

### Confirm scope

Ask the user (or accept from invocation context) which scope to
review. Default to "working tree diff vs. base branch" if unclear.

| Mode | Trigger examples | Behaviour |
|---|---|---|
| **commit** | "commit abc1234", "charter-review the last commit" | One commit (`git show <sha>`) |
| **range** | "commits abc1234..def5678" | All commits in the range |
| **working-tree** | "what I have now", "before I commit", default | `git diff` + untracked files |
| **pr** | "PR #42", "#42" | Fetch via `gh pr diff` and review |
| **phase** | "Phase 1 deliverable", "Phase 4b deliverable" | All files for that phase per `docs/01-PROGRESS.md` (use the file checklist to derive paths) |
| **files** | "review `crates/teslafat/src/nbd/transmission.rs`" | Specific files |

---

## Phase 1 — Pre-flight automated gates

Charter-review starts by running every automated check the charter
prescribes. If these are red, **stop and report immediately** — fix
the automated failures before doing manual review (no point
hand-reviewing code that won't compile / fails lints).

### Rust gates

If any Rust files are in scope:

```bash
cargo fmt --all -- --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-targets --all-features
cargo llvm-cov --fail-under-lines 80 \
    --include-files 'rust/crates/teslafat/src/fs/**' \
    --include-files 'rust/crates/teslafat/src/nbd/**' \
    --include-files 'rust/crates/teslausb-worker/src/indexer.rs' \
    --include-files 'rust/crates/teslausb-worker/src/cleanup.rs'
cargo deny check
cargo machete
cargo doc --no-deps --document-private-items --all-features
```

Any non-zero exit = block. Report which gate failed with output.

### Python gates

If any Python files are in scope:

```bash
ruff check .
ruff format --check .
mypy web/
pytest --strict-markers --strict-config --cov=web --cov-fail-under=80
vulture web/ --min-confidence 80
bandit -r web/ -ll
```

Any non-zero exit = block.

### Pre-commit hooks

```bash
pre-commit run --all-files
```

If this hasn't been installed yet (Phase 0 deliverable), note it
and skip — but include "pre-commit not installed" as a Phase 0
gap in the report.

---

## Phase 2 — The Five Pillars (manual review)

For each changed file, walk through the pillars in order. Cite
the charter section being enforced for every finding. Severity:

- **Blocker** — must fix before merge. Charter breach with no
  acceptable mitigation.
- **Major** — should fix before merge. Charter breach with a
  documented mitigation (e.g., ADR justifying an exception).
- **Minor** — fix opportunistically. Style or naming nit that
  the charter mentions but doesn't strictly forbid.
- **Nit** — purely cosmetic. Optional.

### Pillar 1: No Code Smells (charter §1)

For every changed file, check:

- [ ] **Function length** — any function over 50 SLOC?
  - Use `grep -n '^\(fn\|def\|pub fn\|async fn\) ' <file>` to find
    function starts; count lines between starts.
  - **Blocker** if any function > 50 SLOC without an ADR exception.

- [ ] **Nesting depth** — any code with `if` inside `if` inside `if`,
  or equivalent `for`/`match` combinations, deeper than 3 levels?
  - Visual inspection on the diff is fine for small changes;
    `tokei` or `lizard` for bulk audits.
  - **Major** if > 3 levels without justification.

- [ ] **Magic values** — any bare numbers or strings in logic?
  - `grep -n -E '\b[0-9]{2,}\b' <file>` finds candidates; review
    each. Numbers that ARE just data (array indices 0/1, math
    constants) are fine; numbers with semantic meaning
    (`timeout = 30`, `max_retries = 5`) MUST be named consts.
  - **Major** for unnamed semantic literals.

- [ ] **God modules** — any file > 500 lines (target < 300)?
  - `wc -l <file>` — block at > 500 unless justified by ADR.
  - **Blocker** > 500, **Major** 300-500.

- [ ] **Duplication** — does the diff add code that already exists
  elsewhere in the workspace?
  - For Rust: search `crates/` for similar function signatures
    or repeated patterns.
  - For Python: search `web/` for similar logic.
  - **Major** on third duplication; **Minor** on second.

- [ ] **Primitive obsession** — are `u32`, `String`, `PathBuf` used
  where a newtype would communicate meaning?
  - Example: `fn read(offset: u64, len: u32)` is fine; the names
    carry the meaning. But `fn enqueue(file: String, priority: u8)`
    cries out for `Priority(u8)` newtype.
  - **Minor** unless the primitive is used across module boundaries.

- [ ] **Data clumps** — three+ parameters that always travel
  together but aren't a struct?
  - **Major** if the same triple appears in 3+ call sites.

- [ ] **Comments explaining "what"** — every `//` or `#` comment
  must explain WHY, WHY NOT, or document an invariant. Comments
  that describe what the next line does are noise.
  - **Minor** for individual offenders; **Major** if the file
    is comment-heavy with what-comments.

- [ ] **Cyclomatic complexity** — clippy will flag this for Rust
  (`cognitive_complexity` lint, threshold 25). For Python, use
  `radon cc -nb <file>` and flag anything rated D or worse.
  - **Major** any D/E/F rating; **Blocker** any F.

### Pillar 2: Best Architecture Practices (charter §2)

- [ ] **Hexagonal layering** — does the change respect the four-layer
  rule (Layer 1 domain → 2 services → 3 adapters → 4 entry points)?
  - Specific Rust violations: `crates/teslausb-core/src/sei/`,
    `crates/teslafat/src/fs/`, `crates/teslafat/src/retention.rs`,
    `crates/teslafat/src/cluster_map.rs` MUST NOT import
    `tokio::net`, `std::process`, `tokio::fs`, `rusqlite`, or
    anything else with I/O semantics.
  - Specific Python violations: anything in `web/teslausb_web/services/`
    MUST NOT import `flask`; it MUST be callable from a non-Flask
    context.
  - **Blocker** for any layer-up import.

- [ ] **Dependency inversion** — does the new code construct its
  dependencies directly (`Foo::new()` that internally calls
  `Bar::open()`) or accept them via parameters?
  - **Major** if a new type instantiates its own infrastructure
    deps instead of accepting them.

- [ ] **SRP** — does each module do one thing? A new file named
  `utils.rs` or `helpers.py` is a red flag — `utils` is not a
  responsibility.
  - **Major** for new `utils`/`helpers` files; **Blocker** if
    the module mixes 3+ unrelated concerns.

- [ ] **Composition over inheritance** — Python: no class hierarchies
  > 2 levels deep. Rust: no enum-with-data carrying 6+ variants where
  a trait would be clearer.
  - **Major** for 3+ level Python hierarchies.

- [ ] **Immutability** — does the code mutate where a return-new-value
  would be just as clear? `&mut self` on a method that only needs
  `&self` reads is a smell.
  - **Minor** in most cases; **Major** if the mutation is shared
    state across tasks/threads.

- [ ] **Pure functions where possible** — does the new code mix I/O
  with logic? Pure function carved out + adapter calling it is the
  charter's pattern.
  - **Major** if a new "logic" function takes a file path or
    database handle and does both I/O and computation.

### Pillar 3: No Shortcuts (charter §3)

Cross-reference the charter's shortcut table:

| Shortcut | Required alternative | Severity if found |
|---|---|---|
| `unwrap()` / `expect()` on result that could realistically fail | `?` propagation or specific error handling | **Blocker** (Rust); **Major** (Python equivalent: bare `except`) |
| `print!`/`println!` / Python `print()` | `tracing::info!` / `logging.getLogger` | **Blocker** |
| `panic!`/`assert!` for runtime errors | `Result` return | **Blocker** |
| `# type: ignore` / `Any` | Specific type or `# type: ignore[specific-rule]` with comment | **Blocker** without comment, **Major** with comment |
| `unsafe` block without `// SAFETY:` comment | Add comment justifying every invariant | **Blocker** |
| `TODO`/`FIXME` without linked issue | `# TODO(#123): ...` with linked GitHub issue | **Major** |
| Commented-out code | Delete it (`git` remembers) | **Blocker** |
| "It works, ship it" — no test for new public function | Tests required | **Major** for non-trivial; **Minor** for one-liners |
| "I'll add the test later" | Add it now | **Blocker** |
| Catching `Exception:` / `Box<dyn Error>` at module boundary | Specific error types | **Blocker** at public boundary, **Major** internal |
| `time.sleep` / `tokio::time::sleep` as a synchronisation primitive | Use channels/condvars | **Major** |
| Magic retry counts and timeouts buried in code | Named const at top of file, ideally in config | **Major** |
| Boolean parameters that change behaviour | Enum variants | **Major** |

### Pillar 4: Fix Bugs Immediately (charter §4)

- [ ] **Bug fix without regression test** — every bug fix MUST add a
  test that fails before the fix and passes after.
  - **Blocker** if a fix lands without a regression test.

- [ ] **Boy Scout rule** — if the diff touches a file with adjacent
  smells (e.g., a 200-line function next to a 5-line bug fix), are
  the adjacent smells either fixed or filed as issues?
  - **Minor** for not-fixed; **Major** for not-filed.

- [ ] **TODO sweep** — are any new `TODO` comments added without a
  linked issue? Are any old `TODO` comments next to the diff that
  should be acted on now?
  - **Major** for new unlinked TODO; **Minor** for not-actioned
    adjacent old TODO.

### Pillar 5: No Dead Code (charter §5)

- [ ] **Unused imports** — Rust: `cargo machete` + clippy
  `unused_imports`. Python: ruff `F401`.
  - **Blocker** (CI should catch, but verify locally).

- [ ] **Unused functions/methods** — Rust: `cargo +nightly udeps`
  (if available); Python: `vulture web/ --min-confidence 80`.
  - **Major** any unused public API; **Blocker** if added in
    this PR.

- [ ] **Unused parameters** — Rust: `#[allow(unused)]` is a
  blocker without ADR. Python: ruff `ARG001-ARG005`.
  - **Major**.

- [ ] **Commented-out code** — `grep -nE '^\s*(//|#)\s*(let|fn|def|class|use|import)' <file>`
  - **Blocker** — delete it.

- [ ] **Empty modules / placeholder files** — anything that has no
  exports and isn't a known scaffold (`mod.rs` is OK, even when
  thin).
  - **Major** for new empty modules outside scaffolding.

- [ ] **Vestigial config** — any new TOML key that no code reads?
  Any old key removed from code but still in the example config?
  - **Major** if added; **Minor** if existing.

- [ ] **"Backup" / "old" files** — anything named `*.bak`,
  `*_old.*`, `*_v2.*`, `*_new.*`?
  - **Blocker** — must not be committed.

---

## Phase 3 — Language-specific rules

### Rust deep-dive (charter §"Rust Standards")

For every Rust file in the diff:

- [ ] **`[lints]` config** — does the crate's `Cargo.toml` have the
  charter's `[lints.rust]` and `[lints.clippy]` blocks? Specifically:
  - `unsafe_code = "deny"`
  - `clippy::unwrap_used = "deny"`
  - `clippy::expect_used = "warn"`
  - `clippy::print_stdout = "deny"`
  - `clippy::print_stderr = "deny"`
  - `clippy::dbg_macro = "deny"`
  - `clippy::all = "deny"`
  - `clippy::pedantic = "warn"`
  - `clippy::cognitive_complexity = { level = "deny", priority = -1 }` with threshold 25
  - **Blocker** if any are missing.

- [ ] **`thiserror` for library errors, `anyhow` only at binary outer layer**
  - Domain crates (`teslausb-core`, fs/, sei/) use `thiserror`-derived
    typed errors. Only `main.rs` of each binary may use `anyhow::Result`.
  - **Blocker** for `anyhow::Result` in library code.

- [ ] **`tracing`, not `println!`/`eprintln!`**
  - Any `println!`, `eprintln!`, `print!`, `eprint!`, `dbg!` →
    **Blocker**.

- [ ] **No `static mut`, no `lazy_static!` for mutable state**
  - Use `OnceLock` for immutable global init, `Mutex`/`RwLock` for
    mutable, or pass state via dependency injection.

- [ ] **Public API doc comments** — every `pub fn`, `pub struct`,
  `pub enum`, `pub trait` has a `///` doc comment.
  - **Major** for missing doc; **Minor** for one-liner public items
    that are self-evident.

- [ ] **`#[must_use]` on builders, on `Result`-returning functions
  that callers might accidentally ignore**
  - **Minor** suggestion to add.

- [ ] **`Send`/`Sync` discipline** — any new `Arc<T>` where `T: ?Send`?
  - **Major** if it crosses an `await` point.

- [ ] **Async correctness** — any blocking I/O inside an async
  function? (`std::fs` instead of `tokio::fs`, blocking SQLite call,
  blocking lock?)
  - **Blocker** if it blocks the tokio runtime.

### Python deep-dive (charter §"Python Standards")

For every Python file in the diff:

- [ ] **`pyproject.toml` ruff config** — ruff rule families enabled
  per charter: at minimum `E`, `F`, `W`, `C90`, `I`, `N`, `D`, `UP`,
  `ANN`, `S`, `BLE`, `FBT`, `B`, `A`, `COM`, `C4`, `DTZ`, `EM`,
  `EXE`, `ISC`, `ICN`, `G`, `INP`, `PIE`, `T20` (no print!), `PT`,
  `Q`, `RSE`, `RET`, `SLF`, `SIM`, `TID`, `TCH`, `ARG`, `PTH`,
  `ERA` (no commented-out code!), `PD`, `PGH`, `PL`, `TRY`, `FLY`,
  `NPY`, `RUF`.
  - **Blocker** if `pyproject.toml` doesn't enable T20, ANN, ERA.

- [ ] **`mypy --strict`** with `disallow_any_explicit = true`
  - **Blocker** if config relaxed.

- [ ] **`from __future__ import annotations`** at top of every module
  - **Major** if missing.

- [ ] **`typing.Any` usage** — any `Any` in new code? It must have
  an inline justification comment.
  - **Major** without justification.

- [ ] **`print()` calls** — none, ever, in production code. Use
  `logging.getLogger(__name__)`.
  - **Blocker**.

- [ ] **Bare `except:`** — banned. Catch specific exception types.
  - **Blocker**.

- [ ] **`assert` for production logic** — banned (asserts can be
  optimised away with `-O`). Use explicit `if not x: raise`.
  - **Blocker** for asserts in non-test code (excluding type-narrowing
    `assert isinstance(x, T)` which mypy needs).

- [ ] **`datetime.now()` without `tz=`** — ruff DTZ catches this.
  Always pass `tz=timezone.utc` (or another explicit tz).
  - **Major**.

- [ ] **Public function docstrings** — every `def` exported from a
  module has a docstring (D rule family).
  - **Minor** for missing.

---

## Phase 4 — Architectural compliance (charter §"Architectural Principles")

For every change:

- [ ] **The Layering Rule** — repeat from Pillar 2 with concrete
  module names:
  - Does any file in `rust/crates/teslausb-core/src/{sei,fs,retention,cluster_map}/`
    import `tokio::net`, `std::process`, `rusqlite`, `notify`?
    → **Blocker**.
  - Does any file in `rust/crates/teslafat/src/fs/{fat32,exfat}/`
    import `nbd::*`, `backend::dir_tree`? → **Blocker**.
  - Does any file in `web/teslausb_web/services/` import `flask`,
    `werkzeug`? → **Blocker**.

- [ ] **The Boundaries Are Real** — IPC messages between Rust and
  Python:
  - Any new message type added to `crates/teslausb-core/src/ipc/messages.rs`
    has a corresponding Python type stub in `web/teslausb_web/ipc.py`?
    → **Blocker** if not.
  - Is the message schema versioned (envelope contains a version
    field, additive changes only)? → **Blocker** if schema is
    changed in a breaking way without bumping version.

- [ ] **ADR discipline** — does this change meet ANY of the ADR
  trigger criteria?
  - Affects > 1 module?
  - Locks in a new third-party dependency?
  - Changes a protocol or schema?
  - Makes a performance/correctness trade-off?
  - Was contested in review?
  - If YES to any, is there a new `docs/adr/NNNN-title.md`?
    → **Blocker** if missing. New deps in `Cargo.toml` /
    `pyproject.toml` always trigger an ADR.

---

## Phase 5 — Anti-pattern sweep (charter §"Anti-patterns")

Concrete examples the charter rejects. For each, do a project-wide
`grep` (constrained to the diff context) and flag any hits.

- [ ] **"Just suppress the warning"** — any new `#[allow(...)]` /
  `# noqa` / `# type: ignore` without a comment explaining why?
  → **Blocker** without comment.

- [ ] **"It's just a quick fix"** — any new bare `except:` /
  `Box<dyn Error>` catch at module boundary? → **Blocker**.

- [ ] **Stringly-typed code** — any new `dict[str, str]` or
  `HashMap<String, String>` where a struct would carry the schema?
  → **Major**.

- [ ] **Boolean trap** — any new function signature with a `bool`
  parameter that changes behaviour (not "is feature enabled")?
  Use enum variants. → **Major**.

- [ ] **Catch-all retry** — any new generic `retry(times=N)` wrapper
  that hides specific recoverable vs. non-recoverable errors?
  → **Major**.

- [ ] **Magic timeout** — any timeout literal not pulled from
  config or a named const? → **Major**.

- [ ] **Mega-function** — any function that does parse + validate +
  compute + I/O + format + log? Split. → **Major**.

- [ ] **Comment-as-bug-deferral** — `// FIXME: this is wrong but
  works most of the time` — fix it now or file an issue, do not
  ship a known wrong behaviour. → **Blocker**.

---

## Phase 6 — Delegated reviews

### Security review

If the diff touches:
- Subprocess invocation (`std::process::Command`, `subprocess.Popen`)
- File path construction from user input
- Configfs writes (gadget LUN management)
- Network listener setup (NBD socket, Flask binding, nginx config)
- Samba on/off toggle
- WiFi/AP control
- Any code running as root or with `sudo`
- Cloud credentials, rclone config, token refresh
- Lock chime / lightshow / wraps / music / boombox file upload
- Cache-invalidation triggering
- Cleanup worker (anything that deletes files)

→ **Invoke the `security-review` skill** in changed-mode against
the same scope. Charter-review does not duplicate security work;
it ensures security-review is invoked when it should be.

### UI/UX review

If the diff touches `web/teslausb_web/templates/`,
`web/teslausb_web/static/`, or any Jinja-rendering code:

→ Open `docs/05-UI-UX-DESIGN-SYSTEM.md` (when copied from v1) and
walk the pre-merge checklist. Specifically verify:
- No emoji icons (Lucide SVG only)
- CSS custom properties only (no hex literals)
- Dark + light mode tested
- 44×44 touch targets
- Mobile (375px) + desktop (≥1024px) layouts

→ **Major** for any deviation; **Blocker** for emoji / hex literal.

---

## Phase 7 — Phase-gate criteria (charter + plan)

When invoked with `scope = phase`, additionally verify the phase's
acceptance criteria from `docs/00-PLAN.md`. Examples:

- **Phase 1 gate** — `cargo build --release` green on Pi; `nbd-client`
  handshake smoke test passes; `Cargo.toml` has the charter's `[lints]`
  blocks; `teslausb-core` shared lib created; config loader on TOML.
- **Phase 2 gate** — `fsck.vfat` and `fsck.exfat` both clean against
  `/dev/nbd0`; cold-start synthesis ≤ 1 s for 10K-file tree;
  byte-identical `cmp` of files via mount vs. backing path.
- **Phase 3 gate** — power-cut simulation harness passes; cluster_map
  rebuild byte-identical after restart.
- **Phase 4b gate** — SEI parser parity test vs. v1 fixture set
  byte-identical; cleanup worker preserves GPS-tagged clips and
  reaps no-GPS clips per policy.
- **Phase 4c gate** — hardware test: upload chime → Tesla plays new
  chime on next lock event within 3 s; rapid burst coalesces to one
  invalidation; LUN-0 recording uninterrupted during LUN-1 invalidation.
- **Phase 5 gate** — UI parity screenshot diff (v1 vs. B-1) at
  375px + 1280px, dark + light mode — zero visible diff except
  documented mode-removal edits.
- **Phase 6 gate** — `setup.sh` on clean Pi OS Lite Bookworm → all
  services healthy in < 60 s; `uninstall.sh` returns the Pi to a
  near-vanilla state.

**If the phase's acceptance criteria are not met, the entire
charter-review concludes with status `BLOCKED — phase incomplete`,
even if individual file reviews are clean.**

---

## Phase 8 — Report

Output a structured report. If `scope = pr`, post via
`gh pr review --comment` (multi-paragraph review body). Otherwise
write to `~/.copilot/session-state/<session-id>/files/charter-review-<timestamp>.md`
and surface the path to the user.

### Report template

```markdown
# Charter Review — <scope description>

**Scope:** <commit / range / pr / phase / files>
**Reviewer:** charter-review skill (B-1)
**Date:** <ISO date>
**Charter version:** docs/03-CODE-QUALITY-CHARTER.md @ <git sha>

## Summary

- **Blockers:** N
- **Majors:** N
- **Minors:** N
- **Nits:** N
- **Automated gates:** PASS / FAIL (lint / type / test / coverage / deny)

**Verdict:** APPROVED / APPROVED WITH NITS / CHANGES REQUESTED / BLOCKED

## Automated gate results

| Gate | Status | Notes |
|---|---|---|
| `cargo fmt` | ✅ | |
| `cargo clippy -D warnings` | ❌ | 3 warnings, see findings |
| ... | | |

## Findings

For each finding, in priority order (Blocker → Major → Minor → Nit):

### [BLOCKER] <one-line title>
**File:** `path/to/file.rs:LINE`
**Charter section:** §N — Pillar M, rule "..."
**Issue:** <what's wrong>
**Why it matters:** <how it violates the charter, what could go wrong>
**Required action:** <specific fix>
**Diff suggestion** (optional):
```rust
// before
...
// after
...
```

---

(repeat for each finding)

## Delegated reviews

- Security: invoked? scope? findings?
- UI/UX: invoked? findings?

## Phase-gate status (if scope = phase)

- [x] Criterion 1 — verified
- [ ] Criterion 2 — NOT MET, see Blocker #N
- ...

## Recommended next actions (in order)

1. ...
2. ...
3. ...
```

---

## When to STOP and ASK

This skill autonomously enforces the charter. It does NOT make
judgment calls that override the charter. If the diff contains
something that LOOKS like it should be flagged but the charter
is silent, do this:

1. Note the pattern in the report under "Charter-silent
   observations" — describe it neutrally.
2. Surface it to the user with a question:
   "The charter doesn't currently cover X. The diff does Y.
    Should the charter be amended?"
3. If the user wants the charter amended, ALSO open a PR to
   `docs/03-CODE-QUALITY-CHARTER.md` adding the rule.

**Never silently rubber-stamp** something the charter forbids
because "it's a small case." The charter is binding; exceptions
go through ADRs.

**Never block** something the charter doesn't forbid because
"reviewer's intuition." Add a Charter-silent observation; raise
it for discussion; charter wins until amended.

---

## Performance note

A full charter review on a multi-file PR can take 10-20 minutes
of analysis. For working-tree diffs during active development,
the user may invoke `charter-review --fast` which:
- Skips automated gates (assumes the dev runs them locally)
- Reads only the diff hunks, not full files
- Reports only Blockers + Majors

`--fast` is for pre-commit sanity. Real PR review always runs full.
