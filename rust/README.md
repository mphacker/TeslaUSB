# `rust/` — TeslaUSB B-1 Rust workspace

Cargo workspace for the full-Rust service layer of the B-1 reset
(`SPEC.md` §4, §6). Today it houses one crate; the service binaries
are added as they are built.

```text
rust/
├── Cargo.toml             workspace root: members, shared package metadata, lints, profiles
├── rust-toolchain.toml    pinned stable Rust
├── deny.toml              cargo-deny config (licences, advisories, source allow-list)
└── crates/
    └── teslausb-core/     shared domain types: raw exFAT read/parse path + Tesla SEI extractor
```

`teslausb-core` is pure logic with no I/O dependencies. It carries
the retained raw-reader (`fs::exfat::parse`, boot-sector/directory
decoders, MBR, geometry) and the Tesla SEI decoder (`sei`) that
`scannerd`/`indexd` reuse. The kernel mass-storage gadget owns the
car-facing write path; nothing here touches it.

Forthcoming service crates (added under `crates/` as built):
`gadgetd`, `scannerd`, `indexd`, `webd`, `uploadd`, `retentiond`,
`wifid` (`SPEC.md` §4).

## Build commands

From the repo root:

```bash
cd rust
cargo build --workspace
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all --check
cargo deny check
```

Release builds for the Pi Zero 2 W are cross-compiled on the host
(`--target aarch64-unknown-linux-gnu`); the device is never built on
(`SPEC.md` §5, `setup.md` §5).

## Integration tests

`cargo test` runs unit tests against in-process fakes. A fake cannot enforce a
contract it does not know about, so cross-process wire behaviour is covered
separately by scripted tests under `tests/`, which drive real daemon processes
over their real sockets:

| Script | Covers |
| --- | --- |
| `tests/cloud-bridge.test.sh` | archive → indexd queue → uploadd drain → rclone → remote |

Each script documents its own invocation in its header, exits non-zero on
failure, and is run in a Linux container (Unix sockets cannot be created on a
Windows bind mount). These are not wired into `cargo test`; run them directly
when changing the daemons or the contracts between them.

## Lint policy

`[workspace.lints.rust]` and `[workspace.lints.clippy]` in
`Cargo.toml` encode the engineering standards in `SPEC.md` §7. Each
crate opts in via `[lints] workspace = true`; per-crate lint
overrides require an ADR.
