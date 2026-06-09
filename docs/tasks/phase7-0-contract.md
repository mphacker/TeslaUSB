# Phase 7.0 — Installer/Release contract freeze

> Status: **frozen** · Owner: supervisor (cross-lane contract) · Parent specs:
> [`setup.md`](../specs/setup.md), [`migration.md`](../specs/migration.md),
> [`gadgetd.md`](../specs/gadgetd.md), [`storage.md`](../specs/storage.md).
>
> This document freezes the decisions that **Task 7.1** (`setup.sh` + `setup-lib/`)
> and **Task 7.2** (release/artifact pipeline) both depend on, so the two lanes
> cannot drift in ways that only surface at convergence. It was produced after two
> independent adversarial reviews (rubber-duck + GPT-5.5) of the Phase 7 fan-out
> plan; every "FROZEN" item below resolves a blocking finding from those reviews.
> **If a sub-spec disagrees with deployed reality, deployed reality wins and the
> sub-spec gets a follow-up docs fix (noted in §1).**

## 1. Path freeze (deployed reality wins over prose)

The running units and the `gadgetd` binary already encode concrete paths. The
installer and the release pipeline **must** use these exact paths; do not invent
new ones.

| Purpose | FROZEN path | Source of truth |
|---|---|---|
| Service binaries | `/usr/local/bin/<name>` | `deploy/systemd/gadgetd.service` (`/usr/local/bin/gadgetd`) |
| Backing image + data root | `/data/teslausb/` (image `/data/teslausb/disk.img`) | `gadgetd` `DEFAULT_IMAGE`, the unit's `--image` |
| Archive / media roots | `/data/teslausb/{archive,media}` | follows the data-root above |
| Config | `/etc/teslausb/config.toml` + `/etc/teslausb/secrets/` | `setup.md` §5/§10 |
| Secrets delivery | systemd `LoadCredential=` (files `0600` root-owned) | `setup.md` §10 |
| systemd units | `/etc/systemd/system/<name>.service` | `deploy/systemd/*` |
| Runtime sockets / tmpfs | `/run/teslausb/` | `gadgetd` `DEFAULT_SOCKET` |
| Release verifier (in-repo, trusted) | `setup-lib/verify-release.sh` | this contract |

**Docs-fix follow-up (non-blocking):** `setup.md` currently references
`/srv/teslausb/{archive,media}` (§4/§7) and `/usr/local/lib/teslausb/` or
`/opt/teslausb/` (§7 step 6). These are **superseded** by the frozen `/data/teslausb`
+ `/usr/local/bin` above. A maintainer should reconcile `setup.md` prose to match;
until then this contract is authoritative for the installer.

## 2. The #1 invariant under the installer (provisioning gating)

`setup.sh` must **never** create, grow, partition, format, move, truncate, or
delete `disk.img`. Only `gadgetd` touches the car-facing write path. `gadgetd
provision` is **idempotent and create-only-if-absent** (`if image.exists() →
no-op`), so the danger is not `setup.sh` calling a mutator directly — it is
**indirectly triggering provisioning on a device that has no image yet.**

**The trap (FROZEN finding):** the current `deploy/systemd/gadgetd.service`
provisions via `ExecStartPre=/usr/local/bin/gadgetd provision …`. If a
non-bootstrap mode (`deploy-app`/`update`/`repair`) installs **and starts** that
unit on a fresh device, `ExecStartPre` will `fallocate` a 4 GiB image + partition
+ format it — a destructive bootstrap **without** `--bootstrap-image`.

**FROZEN unit split** (Task 7.1 implements; gadgetd binary unchanged):

- `gadgetd-provision.service` — `Type=oneshot`, `ExecStart=gadgetd provision …`.
  Installed **and enabled ONLY** by `install --bootstrap-image`. Carries the
  destructive-on-absent provisioning.
- `gadgetd.service` — `Type=oneshot RemainAfterExit=yes`, `ExecStart=gadgetd up`,
  `ExecStop=gadgetd down`, `OOMScoreAdjust=-1000`,
  `After=gadgetd-provision.service`. `up` on a **missing** image fails loudly and
  **never creates** it. Safe to (re)install in any mode.
- `gadgetd-control.service` — `ExecStart=gadgetd serve` (unchanged).

**FROZEN mode rules:**

- `deploy-app` / `update` / `repair` / `rollback`: install/refresh unit **files**
  and app binaries/SPA/config only. They **must not** enable or start
  `gadgetd-provision.service`, and **must not restart a healthy `gadgetd`/
  `gadgetd-control`** (a gadget restart re-enumerates USB and interrupts the car's
  recording). `daemon-reload` is fine; (re)starting only the app services
  (`scannerd`/`indexd`/`webd`/`uploadd`/`retentiond`/`wifid`) is fine.
- `install --bootstrap-image` is the **only** path that enables
  `gadgetd-provision.service` (staged-reboot + post-boot validation per
  `setup.md` §7 step 9). On an in-car device this runs through the hardware-test
  rails. **The modified unit ordering requires on-device re-validation at
  migration M3/M4** before it is trusted in the vehicle — flagged, device-gated.

## 3. Release artifact format (coreutils-native; no `jq` on the Pi)

A release is a gzip tarball that extracts to a directory containing:

```
teslausb-<version>-<triple>/
  bin/                       # service binaries, mode 0755
    gadgetd scannerd indexd webd uploadd retentiond wifid
  spa/                       # built SPA bundle (vite dist/), hashed as a tree
  units/                     # the *.service files installed by step 8
  config/config.example.toml
  SHA256SUMS                 # coreutils format: "<64-hex>␠␠<relpath>" per line
  manifest.env              # flat KEY=value metadata (bash-safe, NO code)
  manifest.json             # OPTIONAL rich metadata for host tooling only
```

**FROZEN: Pi-side verification is coreutils-only.** The installer verifies with
`sha256sum -c SHA256SUMS` (run with the extracted dir as cwd) and a **safe
line-parse** of `manifest.env` — it **never** `source`s `manifest.env` and never
parses JSON in bash. `manifest.json` exists purely for host/CI tooling and is
**not** trusted by the Pi-side path.

### 3.1 `SHA256SUMS`
- One line per shipped file (every `bin/*`, every `spa/**` file, every `units/*`,
  `config/config.example.toml`). Format exactly `"<64 lowercase hex>  <relpath>"`
  (two spaces, coreutils default).
- Relative paths only; **no** leading `/`, **no** `..` segment, **no** absolute or
  symlink escape. The verifier rejects any such entry (path-traversal guard).
- `manifest.env`, `manifest.json`, and `SHA256SUMS` itself are **not** listed in
  `SHA256SUMS` (they are the metadata/verifier inputs).

### 3.2 `manifest.env` (required keys, exact names)
```
RELEASE_VERSION=<semver or tag>
GIT_COMMIT=<full 40-hex sha>
TARGET_TRIPLE=aarch64-unknown-linux-gnu
UNIT_SET_VERSION=<integer>
CONFIG_SCHEMA_VERSION=<integer>
SPA_BUNDLE_SHA256=<64 hex>          # sha256 of the canonical spa tree digest (§3.3)
```
- Keys match `^[A-Z][A-Z0-9_]*$`; values are a single line, no command
  substitution, no newlines. The verifier reads keys with a fixed allow-list and
  ignores/【fails on】 anything else (configurable: unknown key = warn).

### 3.3 SPA bundle hash
`SPA_BUNDLE_SHA256` = sha256 over a **stable** concatenation of the per-file
`SHA256SUMS` entries under `spa/` (sorted by relpath). i.e. it is a digest of the
bundle's file-hash manifest, not of a re-tar (which would be nondeterministic).
The verifier recomputes it from the verified `SHA256SUMS` and compares.

## 4. Trust boundary (FROZEN: integrity by default, not authenticity)

- The hashes prove the artifacts **match the manifest** (integrity / anti-
  corruption). They do **not** by themselves prove the manifest is authentic.
- `--artifact-dir DIR` (local, cloned/copied by a trusted human) is the default
  trusted path. The **verifier itself lives in-repo** (`setup-lib/verify-release.sh`,
  cloned with the repo) and is **never** read from the downloaded artifact — this
  closes the "what verifies the verifier" gap.
- `--manifest-url URL` / `--release TAG` remote fetches **must** be HTTPS; a plain
  `http://` source is refused.
- Authenticity (a detached signature over `SHA256SUMS` + `manifest.env`, verified
  against a repo-pinned public key) is an **optional, documented future hook** —
  `setup.md` §5 already allows "+ optional signature". The verifier exposes a
  `--require-signature` seam that is off by default and, when on, checks
  `SHA256SUMS.sig`. Implementing the signing path is **not** in 7.1/7.2 scope.
- `--allow-unverified` is the **only** way to bypass verification and must print a
  loud warning and require `--yes`.

## 5. Extraction safety (FROZEN)
Before trusting a downloaded/handed tarball, the installer must:
- Extract into a fresh temp dir it owns; never extract over live paths.
- Refuse members with absolute paths, `..`, or symlinks pointing outside the dir
  (`tar` with `--no-same-owner`, and a pre-scan of `tar -tzf` for `^/` or `\.\.`).
- Refuse setuid/setgid bits on extracted files.
- Only after `verify-release.sh` passes does any file move to a system path, each
  via the dry-run-aware mutation wrapper with a `.b1-backup-<ISO>` sidecar.

## 6. The canonical verifier (`setup-lib/verify-release.sh`)
- Single source of truth for verification, **invoked by both lanes**: `setup.sh`
  sources it (`verify_release_dir "<extracted-dir>"`); Lane B's pipeline tests run
  it against `release/fixtures/{good,tampered}/`.
- `set -euo pipefail`; no `eval`; no `source`-ing of untrusted files; fail-closed
  (any missing/garbled input ⇒ non-zero). Exit codes: `0` verified, `2` bad
  usage, `3` missing input, `4` verification failed.
- Provided here with passing fixtures so both lanes build on a proven verifier
  rather than re-implementing hashing logic (avoids two-implementation drift).

## 7. Lane ownership & sequencing (no file collisions)
- **Phase 7.0 (this, supervisor):** `docs/tasks/phase7-0-contract.md`,
  `setup-lib/verify-release.sh`, `setup-lib/tests/verify-release.test.sh`,
  `release/README.md`, `release/manifest.schema.json`,
  `release/fixtures/make-fixtures.sh`, `release/fixtures/{good,tampered}/**`.
  Lands on trunk first.
- **Lane A — Task 7.1:** owns `setup.sh`, the rest of `setup-lib/**`,
  `uninstall.sh`, the unit files under `deploy/systemd/**` (incl. the §2 split),
  installer tests. **Consumes** `setup-lib/verify-release.sh` (does not
  reimplement). Host-test bar: shellcheck clean + bats with a fake-root sandbox +
  a single dry-run-aware mutation wrapper + a test that `--dry-run` invokes **no**
  raw mutator + the **global denylist** scan (§8) + the §8 sentinel + negative
  tests.
- **Lane B — Task 7.2:** owns `release/**` packaging + the manifest **generator**
  (produces `SHA256SUMS` + `manifest.env` + `manifest.json` conforming to §3) +
  build scripts. **Reuses** `verify-release.sh` + the fixtures in its tests.
  Reports cross-compile honestly: **Complete** (real aarch64 artifact produced and
  self-verified), **Partial** (pipeline + manifest proven against fixtures, real
  cross-compile blocked by toolchain), or **Blocked**. A stand-in green is **not**
  7.2 complete. Documented release environment is **Linux / WSL / container**
  (Windows tar mode + line-ending + linker hazards make it a poor release host).

## 8. Invariant tests both lanes owe (FROZEN minimum)
- **Global denylist scan:** no disk/image mutator
  (`dd`, `truncate`, `fallocate`, `mkfs*`, `mkexfat*`, `parted`, `sfdisk`,
  `sgdisk`, `losetup`, `mount`, `umount`, `wipefs`) appears in `setup.sh`,
  `setup-lib/**`, `uninstall.sh`, or any installed **unit file**, except the single
  `gadgetd provision`/`up` delegation lines. The scan covers scripts **and** unit
  files **and** any helper shipped in the artifact.
- **Sentinel (disk.img untouched):** create a fake `disk.img`, record its
  `sha256`+size+mtime+inode, run `deploy-app`, `update`, `repair`, `rollback`, and
  safe `uninstall` in `--dry-run` and (sandbox) real mode, assert all four
  attributes unchanged.
- **Negative tests:** `deploy-app` ignores/refuses `--bootstrap-image`; `update`
  preserves `disk.img`/config/secrets/archive/index; `rollback` never restores
  over `disk.img`; `uninstall` refuses while the gadget is bound and preserves the
  LUN by default; a tampered binary fails without `--allow-unverified`; a malformed
  `manifest.env`/`SHA256SUMS` fails closed.
