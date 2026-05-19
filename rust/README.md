# `rust/` — TeslaUSB B-1 Rust workspace

Cargo workspace housing the three Rust crates that make up the B-1
daemon side. The Python web UI lives separately under `web/` (Phase
0.3 onwards) and talks to the daemon via the IPC envelope defined in
`teslausb-core`.

```text
rust/
├── Cargo.toml             workspace root: members, shared package metadata, lints, profiles
├── rust-toolchain.toml    pinned stable Rust (charter §"Toolchain")
├── deny.toml              cargo-deny config (licences, advisories, source allow-list)
└── crates/
    ├── teslausb-core/     shared domain types (BlockBackend, IPC, Filesystem trait)
    ├── teslafat/          NBD server + FAT/exFAT synthesizer binary
    └── teslausb-worker/   background retention + cloud-sync + indexer binary
```

## Phase 0.2 state

All three crates compile clean as empty skeletons. Real code lands in
phased increments per `docs/00-PLAN.md`:

| Crate            | First populated by | Notes                                                                 |
|------------------|--------------------|-----------------------------------------------------------------------|
| `teslausb-core`  | Phase 1.2          | IPC envelope + `BlockBackend` trait.                                  |
| `teslafat`       | Phase 1.1          | Real CLI / tracing init / NBD listener. Existing draft sources live in `teslafat/` at the repo root and get ported file-by-file (charter §"No code smells" forbids dead code, so the drafts move into the workspace as each Phase 1.x increment lands). |
| `teslausb-worker`| Phase 14           | Worker entry point + retention loop.                                  |

## Build commands

From the repo root:

```bash
cd rust
cargo build --workspace --all-targets         # debug build, all three crates
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
cargo deny check
```

Release builds for the Pi Zero 2 W (`armv7-unknown-linux-gnueabihf`) are
cross-compiled per `docs/00-PLAN.md` Phase H1; the dev-box `cargo build`
above is host-native and serves CI / pre-commit.

## Lint policy

`[workspace.lints.rust]` and `[workspace.lints.clippy]` in
`Cargo.toml` mirror docs/03-CODE-QUALITY-CHARTER.md §"Lints" verbatim.
Each crate opts in via `[lints] workspace = true` in its own
`Cargo.toml`; no per-crate lint overrides are permitted without a
charter exception captured in an ADR.
