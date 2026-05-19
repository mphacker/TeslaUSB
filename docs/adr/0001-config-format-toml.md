# ADR-0001 — Use TOML for `teslafat` config file

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-19 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase 1.1 of B-1 rewrite |
| Commit   | `994fd65` (inc-1.1 implementation) |

## Context

The Phase B-1 design draft at `teslafat/src/config.rs` used YAML
(`serde_yaml`) for the daemon's on-disk config file. The
`teslafat/README.md` flagged this as provisional with the note "drafts
use YAML; will switch" and listed `src/config.rs` as a Phase 1.1
deliverable to port. Phase 1.1 (`docs/00-PLAN.md` row 1.1) explicitly
calls for a *"TOML config loader"*.

The implementation step had to commit to one config format. The
candidates were:

1. **YAML** (status quo, draft inherits this from the v1 codebase's
   `config.yaml`).
2. **TOML** (Cargo / Rust ecosystem default; what the plan specified).
3. **JSON** (universal, but no comments — ergonomics loss for an
   operator-edited file).
4. **Custom INI / `.env`** (lowest dependency footprint but loses
   structured types).

## Decision

**Adopt TOML as the on-disk format for `teslafat`'s config file
(`/etc/teslausb/teslafat.toml`).** Wire it through `toml = "0.8"` +
`serde = { features = ["derive"] }` in `rust/crates/teslafat/`.

The Python v1 codebase's `config.yaml` is untouched by this decision —
it serves a different process tree (Flask web UI + bash scripts) and
has no incentive to migrate. B-1 is a clean-slate rewrite; sharing a
config file across runtime boundaries is not a goal.

## Consequences

### Positive

* **Same syntax as `Cargo.toml`.** Operators and contributors only
  need to know one config syntax for the whole Rust workspace.
* **No YAML-style stringly-typed scalar surprises.** TOML has a
  proper integer / string / boolean / datetime type system, so the
  `cluster_size: Option<u32>` and `socket_mode: Option<u32>` fields
  parse without `serde_yaml`'s well-known coercion quirks (`yes` →
  bool, `0644` → int, etc.).
* **Native octal literal support (`0o644`)** for the IPC socket-mode
  field that lands in Phase 1.2, matching how operators write Unix
  permissions in muscle memory.
* **First-class comments.** `#`-comments survive serialisation and
  are how the installer (`setup.sh`, Phase 6.4) will document each
  field inline. YAML supports comments too; JSON does not.
* **Smaller transitive-dep closure than YAML.** `toml = "0.8"` pulls
  in `serde_spanned` + `toml_edit` + `toml_datetime`; `serde_yaml`
  (now unmaintained as of 2024) pulls in `unsafe-libyaml`. Charter
  §"Lints" denies `unsafe_code` at the workspace level, so a stable
  pure-Rust parser is a better fit.

### Negative

* **Operator unfamiliarity.** Operators who only knew the v1
  `config.yaml` need to learn TOML basics. Mitigation: the installer
  generates the file with inline comments and example values.
* **No multi-document support.** YAML's `---` document separators
  let one file hold multiple records. We don't need that for
  `teslafat.toml` (single-record per file) but it forecloses a use
  pattern that v1 sometimes relied on for fixture files.
* **Schema migration cost.** When config schema changes in later
  phases, the `setup.sh` upgrade path needs explicit TOML migration
  rather than `yq` invocations the v1 codebase used.

### Neutral

* **Validation discipline unchanged.** Whether YAML or TOML, the
  `Config::validate` method enforces the semantic constraints
  (volume size range, power-of-two cluster size, FAT32 label length)
  that no schema-only validator can express.
* **`#[serde(deny_unknown_fields)]` works on both.** Schema strictness
  is independent of the wire format.

## Alternatives Considered

### Stay with YAML

* Lower migration cost (1:1 port of the draft).
* `serde_yaml` is unmaintained as of 2024 (last release April 2024,
  archived). Picking it for a multi-year-life daemon is a known
  liability.
* YAML's whitespace-sensitivity is a tax on operator editing in `vi`
  on the Pi over a flaky SSH connection.

### JSON

* Universal parser support, smallest dependency footprint.
* No comments. The installer needs to ship a separate
  `teslafat.toml.example` to document each field, doubling the
  operator surface area.

### Custom format

* Lowest dependency footprint of all options.
* Reinvents the wheel. Charter §"No shortcuts" forbids "rolling
  one's own" when a well-maintained alternative exists.

## Compliance

Charter §"ADRs" (`docs/03-CODE-QUALITY-CHARTER.md` lines 477–485)
mandates an ADR for decisions that meet ≥ 1 of:

* Affects > 1 module — **yes** (`Config` is read by `main.rs` and
  will be read by future Phase 1.2+ modules).
* Locks in a third-party dependency — **yes** (`toml = "0.8"`).
* Changes a protocol or schema — **yes** (on-disk config format).
* Makes a performance/correctness trade-off — **partially** (toml's
  stricter typing is a correctness gain; tiny parse-speed delta is
  irrelevant for a once-per-startup load).
* Was contested in review — **no** (operator confirmed Rust-first
  direction; YAML→TOML is downstream of that).

Three of the five criteria fire → ADR mandatory. This is that ADR.

## Implementation Reference

* Config struct + loader: `rust/crates/teslafat/src/config.rs`
* Wire-in: `rust/crates/teslafat/src/main.rs` (`Config::load` called
  from `run()` before the "started" sentinel)
* Default path: `/etc/teslausb/teslafat.toml` (overridable via
  `--config <path>` CLI flag)
* Tests: `rust/crates/teslafat/src/config.rs` `#[cfg(test)] mod tests`
  (14 cases covering happy-path parse, schema strictness via
  `deny_unknown_fields`, and all six validation rules).
* Integration test: `rust/crates/teslafat/tests/sentinel.rs`
  verifies the loader through the binary boundary on a fixture file.

## Follow-Up Work

* `setup.sh` (Phase 6.4) writes `/etc/teslausb/teslafat.toml` from a
  template that mirrors this schema. Adding, renaming, or removing
  a field is a schema break; bump the doc header in `config.rs` and
  update `setup.sh` in the same change set.
* If future config sub-trees prove unwieldy in flat TOML (deep
  nesting hurts), revisit and consider a multi-file approach
  (`/etc/teslausb/teslafat.d/*.toml`); not a new format change,
  just an organisation change.
