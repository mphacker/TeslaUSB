# `release/` — TeslaUSB B-1 release artifacts & manifest

This directory holds the **release/artifact pipeline** (Task 7.2) and the
**verification fixtures** shared with the installer (Task 7.1). The authoritative
format and trust rules are frozen in
[`docs/tasks/phase7-0-contract.md`](../docs/tasks/phase7-0-contract.md) — read it
first. This README is the quick reference.

## What a release is

A gzip tarball that extracts to `teslausb-<version>-<triple>/` containing:

| Entry | Purpose | Trusted on Pi? |
|---|---|---|
| `bin/<svc>` | aarch64 service binaries (7) | yes — hashed in `SHA256SUMS` |
| `spa/**` | built Vite/Preact SPA bundle | yes — hashed in `SHA256SUMS` |
| `units/*.service` | systemd units installed by step 8 | yes — hashed |
| `config/config.example.toml` | example config | yes — hashed |
| `SHA256SUMS` | `sha256sum`-format line per shipped file | the integrity input |
| `manifest.env` | flat `KEY=value` metadata (bash-safe) | yes — safe line-parse |
| `manifest.json` | rich metadata for host/CI tooling | **no** — host-only |
| `SHA256SUMS.sig` | optional detached signature | only if `--require-signature` |

### `SHA256SUMS` (contract §3.1)
Coreutils format `"<64 lowercase hex>␠␠<relpath>"`. Relative paths only; no
absolute paths, `..`, or symlinked members. Excludes `manifest.*` and itself.
Verified Pi-side with `sha256sum -c --strict`.

### `manifest.env` (contract §3.2) — required keys
`RELEASE_VERSION`, `GIT_COMMIT` (40-hex), `TARGET_TRIPLE`
(`aarch64-unknown-linux-gnu`), `UNIT_SET_VERSION` (int), `CONFIG_SCHEMA_VERSION`
(int), `SPA_BUNDLE_SHA256` (64-hex; the C-sorted `spa/` lines of `SHA256SUMS`,
hashed — contract §3.3). **Never `source`d** by the verifier; parsed with a
strict allow-list.

### `manifest.json`
Rich superset for host tooling, validated against
[`manifest.schema.json`](./manifest.schema.json). **Not** part of the Pi-side
trust path — do not parse it in bash.

## Verifying

The single canonical verifier is
[`setup-lib/verify-release.sh`](../setup-lib/verify-release.sh) (lives in-repo,
cloned + trusted — never read from the downloaded artifact). Both lanes use it:

```sh
# integrity (default):
bash setup-lib/verify-release.sh path/to/extracted-release
# integrity + authenticity (off by default, fail-closed seam):
VR_SIG_VERIFY_CMD=/path/to/keypinned-verify \
  bash setup-lib/verify-release.sh path/to/extracted-release --require-signature
```

Exit codes: `0` verified, `2` usage, `3` missing input, `4` verification failed.

## Fixtures & tests

`fixtures/make-fixtures.sh` deterministically regenerates
`fixtures/good/` (passes) and `fixtures/tampered/` (a binary changed but
`SHA256SUMS` left stale → must fail closed). The verifier suite
`setup-lib/tests/verify-release.test.sh` asserts every documented code path.
Stand-in artifacts use fixed text bytes; real releases ship aarch64 ELF + the
real SPA build, but the hashing/verification contract is identical.

## Trust boundary (contract §4)

Hashes prove **integrity** (artifacts match the manifest), not **authenticity**.
The verifier-in-repo closes "what verifies the verifier". Remote
`--manifest-url`/`--release` must be HTTPS. Signing is an optional, documented,
fail-closed future hook. `--allow-unverified` is the only bypass and must warn +
require `--yes`.

## Build environment (Task 7.2)

The documented release host is **Linux / WSL / container** — Windows tar modes,
line endings, and the aarch64 cross-linker make it a poor release host. The
pipeline must report **Complete / Partial / Blocked** honestly; a stand-in green
is not a finished 7.2.
