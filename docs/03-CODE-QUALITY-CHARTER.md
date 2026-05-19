# TeslaUSB B-1 — Code Quality Charter

**Status:** Binding. This charter sits alongside `00-PLAN.md`
as a non-negotiable foundation. Every PR is reviewed against
it. CI enforces what can be enforced automatically; the rest
is reviewer discipline.

**Operator directive (2026-05-19), verbatim:**
> *"We also want to make sure we don't have code smells, we
> follow best architecture practices, we don't take shortcuts
> or go with the 'easy' approach when there is a better
> approach that might just take a bit more work. Don't be
> lazy. Never leave bad code or bugs, fix things as they are
> found. No dead code."*

This document operationalises that directive.

---

## The Five Pillars

### 1. No Code Smells

Code smells are objective and enforceable. They are not
opinions. The list below is what we reject:

- **Long functions.** Functions > 50 lines or > 50 SLOC
  (excluding doc comments) must be decomposed. No exceptions
  for "it's just data setup" or "the test needs context."
- **Deep nesting.** > 3 levels of nesting (`if`/`for`/`match`
  inside `if` inside `if`) must be flattened with early
  returns, guard clauses, or extracted helpers.
- **Magic values.** No bare numbers or strings in logic. Every
  literal that has meaning gets a `const` with a name. The
  threshold is "would a reader understand this value's
  meaning without grep?" If no, name it.
- **God modules.** No file > 500 lines (target < 300). Split
  by responsibility, not alphabetically.
- **Duplication.** If the same logic appears in 2+ places,
  factor it. Three-strikes rule for *almost*-identical code:
  unify on the third copy.
- **Primitive obsession.** When a primitive (`u64`, `String`,
  `i32`) has semantic meaning, wrap it in a newtype
  (`ClusterIdx(u32)`, `BackingPath(PathBuf)`,
  `RetentionSeconds(u64)`). Compile-time type safety > runtime
  comments.
- **Data clumps.** When 3+ parameters always travel together,
  they're a struct. Pass the struct.
- **Comments that explain "what."** Comments explain WHY,
  WHY NOT, or invariants. Code that needs a comment to
  explain WHAT it does should be rewritten until it doesn't.
- **High cyclomatic complexity.** Functions with > 10 branches
  get refactored. Clippy's `cognitive_complexity` lint at
  threshold 25 is the hard ceiling.

### 2. Best Architecture Practices

- **Hexagonal architecture (ports & adapters).** The Rust
  daemon's domain core (FAT/exFAT synthesis, cluster_map,
  retention) MUST NOT import from infrastructure (NBD,
  POSIX, tokio runtime). Adapters bridge. Domain code is
  pure logic; trivially unit-testable without I/O.
- **Dependency inversion.** High-level modules depend on
  traits, not concrete types. `Filesystem` is a trait;
  `Fat32` and `Exfat` are impls. `BlockBackend` is a trait;
  `DirTreeBackend` is an impl. Tests inject mock impls.
- **Single Responsibility Principle.** A module does ONE
  thing. `fat32/directory.rs` encodes/decodes directory
  entries — it does NOT also handle FAT chain allocation
  (that's `fat_table.rs`).
- **Composition over inheritance.** Rust doesn't have
  inheritance; we won't simulate it via huge enums-with-data.
  Python: small composable classes, not deep hierarchies.
- **Immutability by default.** Rust: prefer `&T` over `&mut T`;
  prefer owned values returned from pure functions over
  in-place mutation. Python: prefer `dataclass(frozen=True)`
  for value objects; copy-then-modify over mutate-in-place.
- **Pure functions where possible.** A function that takes
  inputs and returns outputs (no side effects, no globals)
  is trivially testable, parallelisable, and reasonable. The
  IO boundary is a thin shell around a pure core.
- **No global mutable state.** No `static mut`, no module-level
  Python singletons holding mutable data. State is passed
  explicitly via struct fields or function arguments.
  Acceptable: `OnceCell`/`LazyLock` for configuration loaded
  once at startup.
- **Errors are values, not exceptions.** Rust: `Result<T, E>`
  with typed errors via `thiserror`; never `panic!` in
  production paths. Python: typed exceptions with a clear
  hierarchy; never bare `except:` or `except Exception:`
  without re-raising or logging the specific failure.
- **Testability is a design constraint, not an afterthought.**
  If code is hard to test, the design is wrong. Rewrite the
  design, don't write integration tests as a workaround.

### 3. No Shortcuts — Pick The Right Approach

When two approaches exist and one is "easier" but inferior:

- We choose the better approach.
- "It works for now" is not a justification.
- "We'll refactor later" is a lie; later never comes.
- "It's only used once" doesn't excuse poor design — code
  read many times costs more than code written once.

**Examples of shortcuts we will NOT accept:**

| Shortcut | Better approach we will take instead |
|---|---|
| `unwrap()` in a hot path because "this can't fail" | Propagate the `Result`; if it truly can't fail, prove it with a typed witness |
| `# type: ignore` to silence a mypy error | Fix the type; if the type system can't express it, redesign |
| `try: ... except Exception: pass` to "make tests pass" | Find and fix the actual failure |
| Hard-coding a path/value because config is "annoying" | Wire it through the config struct |
| Copy-paste a function and edit two lines | Extract the variant part as a parameter |
| `if False:` to disable code "temporarily" | Delete it; git keeps history |
| Manual retry loop with `sleep(1)` instead of fixing the race | Find the actual race; eliminate it with proper synchronization |
| Disable a flaky test with `@pytest.mark.skip` | Fix the test or fix the code; flaky tests rot trust |
| Bump a timeout to "fix" intermittent failures | Find the actual cause of the slowness |
| Suppress a compiler warning with `#[allow(...)]` | Fix the underlying issue |
| Use `Any` / `dyn Any` / `serde_json::Value` as data plumbing | Define proper types |

**The "extra effort" rule:** if Approach B takes 2× the time
of Approach A but is correct, we pick B. If B takes 10× the
time and A is "mostly correct," we still pick B unless we
can articulate exactly why A's gaps are acceptable.

### 4. Fix Bugs Immediately, Never Defer

- **Boy Scout Rule.** Leave code cleaner than you found it.
  If you touch a function with smells, clean it. (Stay
  focused: don't refactor unrelated code in a feature PR;
  open a separate cleanup PR.)
- **Found a bug while doing other work?** Fix it in a
  dedicated commit within the same PR (or a separate PR if
  scope is large). Never merge knowing a bug exists.
- **No TODO/FIXME as bug deferral.** `// TODO: handle edge
  case` is a bug ticket disguised as a comment. Either fix
  it now or file a GitHub issue and link to it:
  `// TODO(#42): handle edge case once X is in place.`
- **Fix root causes, not symptoms.** If a test fails
  intermittently, find the race; don't add a retry. If a
  user-facing bug has a workaround, the bug isn't fixed.
- **Regression tests are mandatory.** Every bug fix lands
  with a test that would have caught the bug. No exceptions.

### 5. No Dead Code

Dead code lies. It implies things are used when they aren't.
It bloats binaries. It confuses contributors. We delete it.

- **Unused imports, functions, constants, modules:**
  deleted. CI fails on them (Rust: `dead_code` warning →
  error; Python: `ruff F401, F841` + `vulture`).
- **Commented-out code:** deleted. Git keeps history;
  comments don't.
- **Unused parameters:** deleted (or prefixed `_` if a
  trait signature requires the slot and we genuinely don't
  use the value).
- **Feature flags that have been "on" for > 30 days:**
  the flag and the disabled branch are deleted.
- **Vestigial config keys** (e.g., from v1 that B-1 no
  longer reads): deleted from `teslausb.toml.example`,
  config loader, and docs.
- **"Backup" files** (`foo.py.bak`, `foo.rs.old`):
  deleted; never commit them.

---

## Rust Standards (the `teslafat` daemon)

### Toolchain

- Pinned in `rust-toolchain.toml` at a specific stable version
  (e.g., `1.85.0` — first stable with edition 2024). Update
  deliberately, never auto-roll.
- `edition = "2024"` in Cargo.toml.

### Lints — `Cargo.toml` `[lints.rust]` and `[lints.clippy]`

```toml
[lints.rust]
unsafe_code = "deny"
missing_docs = "warn"           # docs for public items
# Lint *groups* need `priority = -1` so individual lints (e.g.
# `missing_docs`) can still override their level; cargo rejects equal-
# priority group/lint pairs (clippy::lint_groups_priority).
unused = { level = "deny", priority = -1 }
nonstandard_style = { level = "deny", priority = -1 }
future_incompatible = { level = "deny", priority = -1 }

[lints.clippy]
all = { level = "deny", priority = -1 }
pedantic = { level = "warn", priority = -1 }
cognitive_complexity = "deny"
todo = "deny"                   # no TODO! macro in source
unimplemented = "deny"          # no unimplemented!() in source
expect_used = "warn"            # expect() needs justification
unwrap_used = "deny"            # unwrap is forbidden
panic = "warn"                  # panic! needs justification
indexing_slicing = "warn"       # use .get() instead of [i]
print_stdout = "deny"           # use tracing
print_stderr = "deny"           # use tracing
dbg_macro = "deny"              # never dbg!() in committed code
```

Notes:
- `unsafe_code = "deny"` is hard. If a specific module truly
  needs unsafe (e.g., raw NBD socket FD handling), it gets
  `#[allow(unsafe_code)]` at the module boundary with a
  doc comment explaining why and what invariants the unsafe
  block preserves. Each `unsafe` block additionally needs a
  `// SAFETY: ...` comment.
- `unwrap_used = "deny"` — use `?`, `expect("reason")`, or
  pattern match with a real error path. `unwrap` in tests
  is fine (tests get `#![cfg_attr(test, allow(clippy::unwrap_used))]`).
- `expect_used = "warn"` — every `expect("...")` must have a
  message that explains why it can't fail (an invariant
  proof, not "should never happen").

### Error handling

- Library crates use `thiserror` for typed error enums:
  ```rust
  #[derive(thiserror::Error, Debug)]
  pub enum NbdError {
      #[error("client sent invalid magic: 0x{0:016x}")]
      InvalidMagic(u64),
      #[error("io error: {0}")]
      Io(#[from] std::io::Error),
  }
  ```
- Binary entry points (`main.rs`) may use `anyhow::Result`
  for ergonomic propagation, but only at the outermost layer.
- Never `Box<dyn Error>` in public APIs.
- Errors include enough context to diagnose without re-running
  with extra logging.

### Logging

- `tracing` crate, never `println!`/`eprintln!`/`log`.
- Spans for each NBD request, each FS synthesis call.
- INFO summary lines for periodic work; per-tick events at DEBUG.
- WARN for near-misses (e.g., `cluster_map` rebuild took
  > 500 ms — approaches cold-start budget).
- ERROR for unrecoverable conditions (always paired with a
  panic, retry, or graceful degradation; ERROR without action
  is forbidden).

### Concurrency

- `tokio` current-thread runtime; document why if multi-thread
  is introduced.
- Shared state goes through `Arc<...>` + interior mutability
  primitives appropriate for the access pattern: `RwLock`
  for read-heavy, `Mutex` for write-heavy. Never both.
- No `await` while holding a lock that another task waits on.
- Each `tokio::spawn` is documented with: who owns the
  JoinHandle, what cancels it, what happens on panic.

### Module organisation

- One responsibility per file. `nbd/handshake.rs` does
  handshake; if it grows to handle transmission too, split.
- `mod.rs` files re-export the public surface; no logic.
- Test modules: prefer `#[cfg(test)] mod tests { ... }` at
  the bottom of the file under test (for unit tests on
  private items); `tests/` for integration tests.

### Test discipline

- Every public function has at least one test.
- Every bug fix has a regression test.
- Property-based tests (`proptest`) for parsers and
  serialisers (NBD framing, FAT/exFAT directory entries).
- Coverage gate: ≥ 80% line coverage on
  `rust/crates/teslafat/src/fs/` and
  `rust/crates/teslafat/src/nbd/` (the protocol-critical paths).
  Reported by `cargo llvm-cov`.
- Tests must be deterministic. No sleep-based timing.

---

## Python Standards (the `web/` Flask app)

### Toolchain

- Python pinned at the system Pi version (3.11 on Bookworm).
- `pyproject.toml` is the single source of truth for tool
  config — no `setup.cfg`, no per-tool dotfiles.

### Lints — `pyproject.toml`

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = [
    "E", "W",       # pycodestyle
    "F",            # pyflakes (unused imports/vars)
    "I",            # isort
    "B",            # bugbear (likely bugs)
    "C4",           # comprehensions
    "PIE",          # misc good ideas
    "RET",          # return statement issues
    "SIM",          # simplifications
    "TID",          # tidy imports
    "TCH",          # type-checking-only imports
    "ARG",          # unused arguments
    "PTH",          # use pathlib
    "ERA",          # eradicate commented code
    "PL",           # pylint subset
    "RUF",          # ruff-specific
    "UP",           # pyupgrade
    "ANN",          # missing type annotations
    "S",            # bandit (security)
    "T20",          # no print()
    "BLE",          # no blind except
    "FBT",          # boolean trap (positional bool args)
    "DTZ",          # datetime timezone awareness
    "PT",           # pytest style
    "Q",            # quotes
    "ICN",          # import conventions
    "G",            # logging format
    "LOG",          # logging usage
    "PERF",         # perf antipatterns
]
ignore = [
    # ANN101 (self) and ANN102 (cls) were removed in ruff 0.5+ and
    # are no-ops now. Empty `ignore` documents the intent: no
    # charter-mandated rule suppressions.
]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101", "PLR2004"]   # asserts + magic nums OK in tests

[tool.mypy]
strict = true
warn_return_any = true
warn_unused_ignores = true
warn_unreachable = true
disallow_untyped_defs = true
disallow_any_explicit = true   # ban explicit `Any` typing
no_implicit_optional = true
check_untyped_defs = true
```

### Type discipline

- `mypy --strict` passes with no exceptions.
- `Any` is banned in committed code. If the underlying type
  is truly unknown (e.g., `json.loads`), parse into a
  `TypedDict` or `pydantic.BaseModel` and validate.
- `# type: ignore` requires `[error-code]` and a comment
  explaining why; reviewed in PR.
- All public functions have parameter and return type
  annotations (enforced by ANN rules).

### Logging

- `logging` module, not `print()`.
- One module-level logger per file:
  `logger = logging.getLogger(__name__)`.
- Lazy `%`-style formatting in logger calls
  (`logger.info("synced %d files", n)` not `f"synced {n}"`)
  so disabled levels skip the format work.
- INFO for user-visible state changes; DEBUG for routine
  cycles; WARNING for recoverable degradation; ERROR for
  failures that need attention.

### Error handling

- Specific exceptions, not `Exception`. Catch only what
  you can handle.
- Custom exception classes for domain errors:
  ```python
  class CacheInvalidationError(RuntimeError): ...
  class TeslafatUnavailable(RuntimeError): ...
  ```
- Re-raise with context where appropriate (`raise X from e`).
- `except` blocks log the failure with `logger.exception(...)`
  (which captures traceback) when not re-raising.

### Test discipline

- `pytest` with `--strict-markers` and `--strict-config`.
- Coverage gate: ≥ 80% line coverage on
  `web/teslausb_web/services/` (the logic layer); blueprints can
  be lower but tested via Flask test client.
- Tests live in `tests/` mirroring source tree
  (`tests/services/test_cache_invalidation.py`).
- Fixtures share via `conftest.py`.
- No sleep-based timing in tests; mock time via
  `freezegun` or pass a clock interface.
- HTTP tests use Flask's test client; no real network.
- File system tests use `tmp_path` fixture, never `/tmp`
  directly.

### Dead code detection

- `vulture web/teslausb_web/ --min-confidence 80` in CI
  (scoped to source, not tests — pytest fixtures look unused
  to vulture's static analysis but are called by pytest's
  collector).
- Manually triaged when it flags false positives (rare
  with web frameworks).

---

## Architectural Principles (cross-language)

### The Layering Rule

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Pure domain                                   │
│  - FS synthesis math, cluster map, retention policy     │
│  - No I/O, no time, no globals                          │
│  - 100% unit-testable                                   │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Application services                          │
│  - Orchestrates domain objects to fulfil use cases      │
│  - Depends on Layer 1 + abstract ports                  │
├─────────────────────────────────────────────────────────┤
│  Layer 3: Adapters (infrastructure)                     │
│  - NBD server, POSIX backend, SQLite, Flask routes      │
│  - Implements the abstract ports                        │
│  - Tested via integration tests                         │
├─────────────────────────────────────────────────────────┤
│  Layer 4: Entry points                                  │
│  - main.rs, app.py                                      │
│  - Wires adapters into services into domain             │
└─────────────────────────────────────────────────────────┘
```

Dependencies point downward only. Layer 1 never imports
Layer 2/3/4. Layer 2 never imports Layer 3 (only the trait
defined in Layer 2). Violations are rejected at review.

### The Boundaries Are Real

- The `teslafat` daemon and the Flask `web` app are
  separate processes communicating over an IPC socket.
  They don't share Rust code, Python code, or memory.
  Their contract is the IPC schema (versioned).
- The Rust domain modules don't know NBD exists. The NBD
  layer doesn't know FAT/exFAT exists; it speaks
  `BlockBackend`.
- The web `services/` layer doesn't import Flask. Routes
  are thin shells that call into services.

### Dependency Injection

- Construct objects with their dependencies passed in.
  Don't pull from globals or singletons inside the object.
- Example:
  ```rust
  // BAD
  impl Synthesizer {
      fn new() -> Self {
          Self { backend: get_global_backend() }
      }
  }

  // GOOD
  impl Synthesizer {
      fn new(backend: Arc<dyn BlockBackend>) -> Self {
          Self { backend }
      }
  }
  ```
- This makes testing trivial (pass a mock backend) and
  decouples the lifetime of the object from the lifetime
  of its dependencies.

### ADRs (Architectural Decision Records)

Any decision that meets ≥ 1 of these criteria gets a
lightweight ADR in `docs/adr/NNNN-title.md`:
- Affects > 1 module
- Locks in a third-party dependency
- Changes a protocol or schema
- Makes a performance/correctness trade-off
- Was contested in review

Template:
```markdown
# ADR-NNNN: Title

**Status:** Accepted / Superseded by ADR-XXXX / Rejected
**Date:** YYYY-MM-DD
**Deciders:** @user1, @user2

## Context
What's the problem, what are the constraints, what alternatives
were considered.

## Decision
What we chose, with one-paragraph rationale.

## Consequences
- Positive: ...
- Negative: ...
- Neutral: ...
```

ADRs are append-only; if a decision changes, write a new
ADR superseding the old, don't edit history.

---

## Code Review Checklist

Reviewer signs off only after going through:

- [ ] Charter compliance (lints pass, tests pass, no
      banned patterns)
- [ ] New code has tests; bug fixes have regression tests
- [ ] Public APIs have doc comments
- [ ] No new `TODO` without a linked issue
- [ ] No `unwrap`/`expect`/`Any`/`# type: ignore` without
      justification
- [ ] No commented-out code, no dead branches
- [ ] Names communicate intent (no `data`, `tmp`, `result1`)
- [ ] Function lengths reasonable (no > 50 lines)
- [ ] Module structure respects layering
- [ ] Error handling is specific and actionable
- [ ] Logging at appropriate levels, no leaked secrets
- [ ] Performance impact considered (Pi Zero 2 W budget)
- [ ] Documentation updated if user-visible
- [ ] CHANGELOG entry if user-visible

**Approval requires all checked.** "Looks good to me"
without going through the list is not a review.

---

## CI Gates (must pass before each commit / merge)

**Enforcement venue:** Currently the local gate runner
`scripts/check.sh` (Phase 0.4). The operator runs it before each
commit; the pre-commit framework (Phase 0.5) wires the same
gates into the git hook so they run automatically. A future
GitHub Actions workflow may re-enable cloud enforcement, but it
is intentionally NOT a Phase 0 deliverable — the operator
preference (2026-05-19) is "prefer to not rely on github actions
for now", and full integration testing requires real hardware
(H-phases) which cloud CI can't provide anyway. The gate
definitions below are venue-neutral: same commands, same exit-on-
red rule, wherever they run.

**Installation:** the toolchain (rustup, cargo-deny, cargo-machete,
cargo-llvm-cov, lychee, plus the out-of-tree Python venv with
ruff / mypy / pytest / pytest-cov / vulture / bandit / pre-commit)
is bootstrapped by `scripts/setup-dev.sh` (Phase 0.6) on a clean
dev box. Run `./scripts/setup-dev.sh --check` to audit which
tools are missing without modifying anything, or
`./scripts/setup-dev.sh --dry-run` to see what would happen,
or `./scripts/setup-dev.sh` to install. Optional tools
(cargo-deny, cargo-machete, cargo-llvm-cov, lychee) absent from
the dev box cause `scripts/check.sh` to emit `[SKIP]` lines with
a WARNing rather than failing — install via `setup-dev.sh` to
move them from SKIP to PASS.

For Rust changes (script section `--rust`):
```yaml
- cargo fmt --all -- --check
- cargo clippy --all-targets --all-features -- -D warnings
- cargo test --all-targets --all-features
- cargo llvm-cov --fail-under-lines 80 \
      --include-files 'rust/crates/teslafat/src/fs/**' \
      --include-files 'rust/crates/teslafat/src/nbd/**'
- cargo deny check
- cargo machete    # unused dependencies
- cargo doc --no-deps --document-private-items
```

For Python changes (script section `--python`; run from `web/` so
tool configs in `web/pyproject.toml` resolve correctly):
```yaml
- cd web
- ruff check .
- ruff format --check .
- mypy          # `files = ["teslausb_web", "tests"]` in pyproject.toml
- pytest --strict-markers --strict-config \
         --cov=teslausb_web --cov-fail-under=80
- vulture teslausb_web --min-confidence 80
- bandit -r teslausb_web -ll   # security linter, low+ severity
```

For any changes (script section `--hygiene`):
- All markdown links resolve (lychee — `git ls-files '*.md'`).
- No new tracked files > 1 MiB without LFS approval.
- `git ls-files` contains no `.bak`, `__pycache__`, `target/`,
  `node_modules/`, `.idea/`, `.vscode/` paths. (Local on-disk
  caches are fine — only COMMITTED artifacts are blocked.)

**A red gate run is a blocked commit. Period.** No "I'll fix it
after merge." No "the test is flaky." Fix it first. To run every
gate locally: `./scripts/check.sh --all` (continues past failures
and prints a summary) or `./scripts/check.sh` (fail-fast).

---

## Pre-commit Hooks (`.pre-commit-config.yaml`)

Local enforcement so issues are caught before commit. Phase 0.5
delegates all Rust / Python / hygiene gates to `scripts/check.sh`
so there is a SINGLE source of truth for the gate definitions
(the script). The canonical config lives at the repo root in
`.pre-commit-config.yaml`. Shape:

```yaml
repos:
  # Cheap formatting / safety fixes that have no equivalent in
  # scripts/check.sh. One-time clone; cached thereafter.
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: trailing-whitespace
        exclude: '\.md$'  # markdown trailing spaces are intentional line breaks
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-toml
      - id: check-merge-conflict
      - id: check-added-large-files
        args: [--maxkb=1024]  # 1 MiB cap, same as CI Gates hygiene rule
      - id: detect-private-key
      - id: mixed-line-ending
        args: [--fix=lf]

  # Heavy gates: delegate to scripts/check.sh so the gate definitions
  # are NOT duplicated between YAML and the script.
  - repo: local
    hooks:
      - id: scripts-check-hygiene
        name: scripts/check.sh --hygiene
        entry: ./scripts/check.sh --hygiene
        language: system
        pass_filenames: false
        always_run: true
      - id: scripts-check-rust
        name: scripts/check.sh --rust
        entry: ./scripts/check.sh --rust
        language: system
        pass_filenames: false
        files: '\.rs$|^rust/.*Cargo\.(toml|lock)$|^rust/rust-toolchain\.toml$'
      - id: scripts-check-python
        name: scripts/check.sh --python
        entry: ./scripts/check.sh --python
        language: system
        pass_filenames: false
        files: '\.py$|^web/pyproject\.toml$'
```

Per-tool upstream hooks (`astral-sh/ruff-pre-commit`,
`pre-commit/mirrors-mypy`) are deliberately NOT used — they would
duplicate the gate definitions already in `scripts/check.sh`, and
the local-hook delegation matches the 2026-05-19 operator
preference against cloud-CI / external-clone dependencies.
`pre-commit/pre-commit-hooks` is the only upstream repo, retained
for cheap whitespace/EOF/yaml/TOML fixes that have no equivalent
in the script.

Installed by `setup-dev.sh` (see Phase 0); `pre-commit>=3.7` is
declared as a dev dep in `web/pyproject.toml` so
`pip install -e web/[dev]` brings it in. Operator setup:

```bash
pip install -e web/[dev]   # or just: pip install pre-commit
pre-commit install         # registers .git/hooks/pre-commit
pre-commit run --all-files # verify all hooks pass on the tree
```

---

## Anti-Patterns We Reject (with concrete examples)

These appear in real code reviews. Each is a hard reject:

### "Just suppress the warning"
```rust
// REJECTED
#[allow(clippy::unwrap_used)]
let x = some_option.unwrap();

// REQUIRED
let x = some_option.ok_or(MyError::MissingX)?;
```

### "It's just a quick fix"
```python
# REJECTED — silently swallows errors
try:
    do_thing()
except Exception:
    pass

# REQUIRED — explicit handling
try:
    do_thing()
except SpecificError as e:
    logger.warning("do_thing failed in expected way: %s", e)
    return DefaultValue()
```

### "Stringly-typed"
```python
# REJECTED
def process(status: str) -> None:
    if status == "pending":
        ...
    elif status == "active":
        ...

# REQUIRED
class Status(enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"

def process(status: Status) -> None:
    match status:
        case Status.PENDING: ...
        case Status.ACTIVE: ...
```

### "Boolean trap"
```python
# REJECTED — caller can't tell what True/False mean
fetch_data(True, False, True)

# REQUIRED — keyword-only, or enum
fetch_data(include_archived=True, follow_redirects=False, validate=True)
```

### "Catch-all retry"
```python
# REJECTED — masks real bugs
for _ in range(5):
    try:
        return do_thing()
    except Exception:
        time.sleep(1)
raise RuntimeError("gave up")

# REQUIRED — retry only specific transient errors, with backoff
backoff = exponential_backoff(start=0.5, max=10)
for delay in backoff:
    try:
        return do_thing()
    except (ConnectionError, TimeoutError) as e:
        logger.info("transient failure, retrying in %.1fs: %s", delay, e)
        time.sleep(delay)
raise RuntimeError("retries exhausted")
```

### "Magic timeout"
```rust
// REJECTED
tokio::time::sleep(Duration::from_millis(500)).await;

// REQUIRED
/// How long the NBD handshake may take before we treat the
/// peer as unresponsive and drop the connection.
const NBD_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(5);
tokio::time::sleep(NBD_HANDSHAKE_TIMEOUT).await;
```

### "Mega-function"
```python
# REJECTED — does six things
def handle_upload(request):
    # ... 200 lines: validate, write to disk, encode,
    # persist to DB, invalidate cache, render response

# REQUIRED — composed of small, focused helpers
def handle_upload(request: UploadRequest) -> UploadResponse:
    validated = _validate_upload(request)
    path = _write_to_staging(validated)
    encoded = _encode_if_needed(path)
    record = _persist_record(encoded)
    _schedule_cache_invalidation()
    return _render_response(record)
```

### "Comment-as-bug-deferral"
```rust
// REJECTED
// TODO: handle the case where Tesla sends NBD_CMD_TRIM
match cmd {
    NbdCmd::Read => handle_read(...),
    NbdCmd::Write => handle_write(...),
    _ => Ok(()), // ignore for now
}

// REQUIRED — either implement it, or explicitly reject with a typed error
match cmd {
    NbdCmd::Read => handle_read(...),
    NbdCmd::Write => handle_write(...),
    NbdCmd::Trim => Err(NbdError::Unsupported(NbdCmd::Trim)),
    NbdCmd::Flush => handle_flush(...),
    NbdCmd::Disc => return Ok(()),
}
```

---

## The "Pick the Hard Right" Decision Framework

When you face a choice and one option is faster but worse,
walk through these questions. If the answer points to the
harder option even once, take it.

1. **Will this code be read again?** If yes, optimise for
   clarity over keystrokes.
2. **Is the easy path papering over a real bug?** If yes,
   fix the bug.
3. **Does the easy path violate the layering rule?** If yes,
   take the harder path that respects layers.
4. **Will the easy path require explanation in PR review?**
   If yes, the harder path explains itself.
5. **Would I be embarrassed to point a senior engineer at
   this code in 6 months?** If yes, take the harder path now.
6. **Am I tired and just want this done?** If yes, take a
   break. Decisions made when tired bias toward shortcuts.

The phrase "we can always refactor later" is a smell.
"Later" rarely comes. Do it right the first time.

---

## When Standards Conflict

Charter rules sometimes conflict with each other or with the
operational reality of a Pi Zero 2 W. When they do:

1. **Correctness > performance > readability > brevity.**
   Always pick a more correct option over a faster one,
   unless the performance impact is verified to break the
   Pi Zero 2 W budget.
2. **Safety invariants in `00-PLAN.md` always win.** Power
   safety, USB visibility, watchdog yielding, etc., are
   above code quality concerns.
3. **If a charter rule is genuinely wrong for our context,**
   file an ADR proposing the change. Don't quietly violate it.

---

## Onboarding Contract

Every contributor (including future-me) commits to this
charter implicitly by opening a PR. Reviewers enforce it.
The charter evolves via ADRs, never silently.

If something in this charter feels wrong, propose a change
via ADR. Don't ignore it.
