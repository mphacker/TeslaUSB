# SPEC — Device setup & install (`setup.sh`)

> Parent: [`SPEC.md`](./SPEC.md) · Type: ops / tooling (not a runtime service)
> Audience: **public** (clone-the-repo-and-install) **and** maintainer release.
> Companion to [`migration.md`](./migration.md) (operator in-place conversion)
> and `uninstall.sh`. **Inspired by — not copied from** — v1's `setup-lib/`
> structure (thin orchestrator + numbered idempotent steps + shared helpers).

## 1. Objective & audience

A reproducible, **idempotent/convergent** installer so anyone can clone the repo
and bring the B-1 stack up on their **own** Raspberry Pi, plus a non-destructive
deploy mode the operator migration reuses. `setup.sh` is the **single install
mechanism**; nothing else hand-deploys to a device.

Two supported provisioning modes (these are distinct and must not silently drift):

- **fresh-install (public).** The user supplies a clean 64-bit **Pi OS Lite
  (Bookworm)** on **their own** card and runs `setup.sh install` directly. A
  user installing their own OS on their own card is their choice — this is **not**
  the O1 "no-reflash" constraint, which governs converting an **existing in-car**
  device.
- **deploy-app (operator migration).** Used by [`migration.md`](./migration.md)
  M4 to deploy binaries + SPA + units + config onto a device whose LUN was already
  provisioned and **proven** by `gadgetd` in M3. **Non-destructive** — it never
  touches `disk.img`, partitions, or boot config.

## 2. Relationship to migration & the #1 invariant

- [`migration.md`](./migration.md) is the **operator's own in-car legacy device**
  path; it runs **only** via the hardware-test rails (dead-man reboot, SSH/WiFi/
  boot protected). M3 stands up and **proves** the kernel LUN; **M4 calls
  `setup.sh deploy-app`** — not the destructive full installer.
- **`setup.sh` never partitions, formats, or writes `disk.img` itself.** Creating
  and laying out the car-facing image (fixed-size `fallocate`, MBR + p1/p2 exFAT,
  volume labels) is **`gadgetd`'s** job ([`gadgetd.md`](./gadgetd.md)) — the sole
  owner of the write path (the #1 invariant). `setup.sh` only **authorizes and
  triggers** `gadgetd` first-run provisioning and **verifies** the result. The
  destructive bootstrap is reachable **only** behind an explicit
  `--bootstrap-image` flag, and even then the layout is delegated to `gadgetd`.
- On an **in-car** device, boot/gadget mutations go through the hardware-test
  rails; a **fresh public** install runs directly but keeps the same discipline:
  back up first, stay idempotent, and never break SSH/WiFi/boot.

## 3. Modes / subcommands

| Subcommand | Destructive? | What it does |
|---|---|---|
| `install` | only with `--bootstrap-image` | Full fresh-install (preflight → packages → users → dirs → config → artifacts → units → network → memory → [bootstrap image+gadget+boot via `gadgetd`] → activate). |
| `deploy-app` | no | Binaries + SPA + units + config only. **Never** touches `disk.img`/boot/partitions. Migration M4 uses this. |
| `update` | no (data-preserving) | Converge to a release's versions (new binaries/SPA/units + safe config migrations). Preserves `disk.img`, config, secrets, archive, index. |
| `repair` | no | Re-assert desired state (perms, unit enablement, ownership) without changing data. |
| `rollback` | no | Restore the previous release's artifacts + `.b1-backup-<ts>` sidecars. |
| `uninstall` | guarded | See §9 — car-disconnected, safe-by-default. |

Global flags: `--dry-run`, `--only NN[,NN]`, `--skip NN[,NN]`, `--artifact-dir DIR`,
`--release TAG`, `--manifest-url URL`, `--bootstrap-image` (explicit, destructive),
`--yes`, `--help`. Exit codes are documented (`0` ok/dry-run, `2` bad flags,
`3` missing precondition, `4` step failed).

## 4. Prerequisites, preflight & where hardware gates apply

**Prerequisites:** Pi Zero 2 W (or a documented compatible board); 64-bit Pi OS
Lite (Bookworm); a `dwc2`-capable USB-OTG port; a sudo-capable user; internet for
apt + artifact download.

**Installer local self-checks (every run — these are NOT the operator's spikes):**
device model, OS/arch, kernel `configfs` + `usb_f_mass_storage` availability,
free space vs the storage budget ([`storage.md` §2](./storage.md)), SSH/network
safety, and **artifact presence + integrity** (§5). Refuse with a clear message
if any precondition is unmet.

**Three layers of "gating" (resolves the public-vs-spike tension):**
- **Project release gates (maintainer).** Before a release/installer is published,
  the maintainer must have **PASSed** the relevant hardware-first spikes — *LUN
  acceptance*, *Boot time*, *disk.img sizing*
  ([`hardware-first-development.md`](./hardware-first-development.md)) — on the
  supported hardware. A public cloner does **not** re-run these.
- **Installer self-checks (local).** Cheap, deterministic preflight (above) the
  installer always runs.
- **User acceptance (final).** The car actually recognizes the drive and records
  (§8). Nothing is "done" until this holds.

On **unsupported** hardware the installer **warns** (the release gates were proven
elsewhere) rather than silently proceeding as if proven.

## 5. Artifact & release strategy (no build-on-Pi)

The 512 MB Pi does **not** compile the workspace (too slow, OOM risk). `setup.sh`
installs **prebuilt** artifacts:

- **aarch64 Rust release binaries** (`cargo build --release --target
  aarch64-unknown-linux-gnu`) and a **hashed SPA bundle** (`npm run build`),
  assembled into a release tarball.
- A **release manifest** accompanies the artifacts: release version, **git
  commit**, target triple, **per-binary `sha256`** (+ optional signature), **SPA
  bundle hash**, the systemd **unit-set version**, and the **config-schema
  version**.
- `setup.sh` obtains artifacts from `--artifact-dir`, a `--release` GitHub
  Release, or `--manifest-url`, and **verifies every hash against the manifest**.
  It **refuses** a mismatch unless `--allow-unverified` is given explicitly. This
  closes the "a cloner of `main` has no matching/ trusted binary" gap.

A maintainer (or an advanced user) produces the tarball + manifest via the repo's
documented build/release path (host cross-compile or CI). Building on a desktop
and copying to the Pi is supported; building **on** the Pi is not.

## 6. Design (inspired by v1, not copied)

- **Thin orchestrator** that sources **numbered, single-purpose step files** in
  order, each exposing one idempotent function; a **shared helpers lib**
  (structured logging, dry-run-aware command runner, first-touch
  `.b1-backup-<ISO>` sidecars, apt/unit/user/verify helpers). `--dry-run` /
  `--only` / `--skip`; root (or dry-run) precondition; documented exit codes.
- **Convergent idempotency — not "always a no-op".** Each step detects the
  current state/version and **converges** to the desired state. Re-running an
  unchanged `install` is a no-op, but `update` **applies safe migrations**.
  **Destructive changes** (repartition/reformat/`disk.img` resize) never happen
  without an explicit flag; `disk.img`, config, secrets, archive, and index are
  **preserved by default**.

## 7. Step list (new architecture — no nbd / nginx / python)

Ordered; each idempotent/convergent and dry-run-aware:

1. **preflight** — model / OS / arch / `configfs` / free-space / artifact
   integrity (§4, §5).
2. **packages** — minimal: `exfatprogs` (mkfs/fsck.exfat), `dosfstools` +
   `parted`/`sfdisk` (MBR), **`hostapd` + `dnsmasq`** (`wifid` AP onboarding —
   these **are** used per [`wifid.md` §2](./wifid.md)), a network manager
   (NetworkManager *or* systemd-networkd — pick one and be consistent), and
   `watchdog` (optional hardening). **Explicitly NOT** `nginx`, `python*`, or
   `nbd-*` — see [`SPEC.md` §10 NEVER](./SPEC.md).
3. **users** — a `teslausb` system user/group and a tight
   `/etc/sudoers.d/teslausb` fragment; service-account model per §10.
4. **data-roots** — `/data/teslausb/{archive,media}` (+ the `disk.img` path),
   `/var/lib/teslausb` (SQLite), `/run/teslausb` (tmpfs for sockets), with correct
   ownership/modes ([`SPEC.md` §6.1](./SPEC.md)).
5. **config** — install an example `/etc/teslausb/config.toml` (schema-versioned)
   + per-service secret files; **secrets `0600` root-owned**, delivered to
   services via systemd `LoadCredential=` (§10). Never committed, logged, in the
   SPA bundle, or on the Tesla volume.
6. **binaries** — install the verified aarch64 service binaries to
   `/usr/local/bin` (contract §1; the frozen deployed path).
7. **spa** — install the verified hashed SPA bundle that `webd` serves.
8. **units** — install the **7** service units (`gadgetd`, `scannerd`, `indexd`,
   `webd`, `uploadd`, `retentiond`, `wifid`) with cgroup `MemoryMax`,
   `gadgetd OOMScoreAdjust=-1000`, and the canonical OOM order
   (`uploadd → wifid → webd → scannerd → retentiond → indexd → NEVER gadgetd`,
   [`SPEC.md` §7](./SPEC.md)); `daemon-reload`; **enable** but defer start to
   `activate`.
9. **image + gadget + boot (destructive bootstrap; `gadgetd`-owned)** — reachable
   **only** via `install --bootstrap-image`: **trigger `gadgetd` first-run
   provisioning** of `disk.img` (fixed-size `fallocate`, MBR + p1/p2 exFAT, volume
   labels, the TeslaCam bootstrap rule §8); enable `dwc2` (config.txt overlay +
   modules-load) with `.b1-backup` sidecars. **Staged-reboot model:** mutate boot
   with backup → reboot → **post-boot validation** (UDC `state`, `configfs` LUN
   present) → printed rollback/rescue instructions if validation fails. On an
   in-car device this runs through the hardware-test rails, never bare.
10. **network** — `wifid` STA config + the WPA2 AP (`hostapd` + `dnsmasq`) for
    onboarding. Network changes are **staged with rollback** so a bad change can't
    strand a headless device.
11. **memory** — prefer **zram**; the primary controls are bounded `MemoryMax` +
    the OOM order. Any **SD-card swap** is **opt-in** and **gated on the microSD
    contention spike** (writing swap to the card competes with the car write
    path — [`hardware-first-development.md`](./hardware-first-development.md)).
12. **hardening (opt-in)** — read-only root + overlay/tmpfs and the hardware
    watchdog (`/dev/watchdog`), matching [`migration.md` M5](./migration.md).
13. **activate** — enable + start units in dependency order (`gadgetd` first),
    run a post-start health check, and print first-run onboarding info (§11).

## 8. Tesla-volume bootstrap rule

p1 is a **blank exFAT** volume created by `gadgetd`. Whether the car
auto-creates `TeslaCam/` on a blank volume **or** requires a seeded **empty
top-level `TeslaCam/` marker** is **resolved by the LUN-acceptance spike**
(unknown #1, [`tesla-usb-contract.md`](./tesla-usb-contract.md)). If the spike
shows a marker is required, `gadgetd` seeds **only** the empty top-level
`TeslaCam/` directory — it **never** creates the car's subfolders
(`RecentClips/`, `SavedClips/`, `SentryClips/`), which the car owns
([contract §4](./tesla-usb-contract.md)). p2 media folders use the **exact**
contract names. The drive being recognized (car records) is the final acceptance.

## 9. Uninstall — safe by default

A companion `uninstall.sh`:

- **Refuses** to run if the gadget is **bound/active** (the car may be using the
  drive). Requires the car **disconnected** + explicit confirmation + a backup
  first.
- **Default safe mode:** stop + disable the **app** services but leave `gadgetd`,
  the LUN, and boot config **intact**, so the drive keeps working.
- `--full` (car-disconnected only) reverses boot/gadget/users/packages back to the
  captured `.b1-backup` baselines. Never deletes `archive/`, `media/`, or clips
  without an explicit `--purge-data`.

## 10. Config, secrets & service accounts

- Services run as the unprivileged `teslausb` account **where possible**;
  `gadgetd` and `wifid` need privilege for `configfs`/netlink and secret access.
- **Secrets** (cloud OAuth/rclone tokens, WiFi PSK + AP passphrase, Samba creds)
  are stored **`0600` root-owned** and delivered to the owning service via systemd
  **`LoadCredential=`** — this preserves the [`SPEC.md` §7](./SPEC.md) /
  [`wifid.md` §2](./wifid.md) "root-only `0600`" rule **and** lets a non-root
  service read only the secret it needs. Nothing secret is world-readable, logged,
  shipped in the SPA bundle, or written to the Tesla volume.

## 11. First-boot / onboarding UX

A headless installer must leave the device reachable:

- A stable hostname / **mDNS** name (e.g. `<name>.local`).
- If home WiFi isn't configured, `wifid` starts the **WPA2 AP** with a **generated
  passphrase** that `activate` prints (and that is shown on any attached console).
- The captive portal (`webd /portal`, served over the AP — [`wifid.md` §5](./wifid.md))
  walks the user through WiFi + initial config. Network changes are **staged with
  rollback** so a failed change can't strand the device.

## 12. Acceptance criteria

- [ ] `setup.sh install --dry-run` on a clean supported Pi prints every action and
      mutates nothing.
- [ ] `install` brings all **7** services healthy, `webd` reachable, and the **car
      recognizes the drive and records** (final acceptance, §8).
- [ ] Re-running `install` is a no-op; `update` converges to a new release
      **without** destroying `disk.img`/config/secrets/archive/index.
- [ ] No `nginx`/`python`/`nbd` installed; artifacts **verified against the
      manifest** (or explicitly `--allow-unverified`).
- [ ] Secrets `0600` root-owned, delivered via `LoadCredential`; none in the
      bundle or on the Tesla volume.
- [ ] `uninstall` **refuses** while the gadget is bound; safe-default keeps the
      LUN alive.
- [ ] SSH/WiFi/boot survive `install` on supported HW; boot-config changes are
      backed up and reversible.

## 13. Boundaries

**ALWAYS** be idempotent/convergent; back up (`.b1-backup`) before overwriting
anything outside our tree; verify artifacts against the manifest; keep `gadgetd`
the **sole** owner of `disk.img`/the write path; preserve user data on `update`;
keep secrets out of the bundle and the Tesla volume; on an in-car device install
via the hardware-test rails.
**ASK FIRST** before any destructive `disk.img`/partition/boot change; before
running on unsupported hardware; before adding a heavyweight dependency.
**NEVER** build on the Pi; never let `setup.sh` (rather than `gadgetd`)
format/partition the LUN; never install `python`/`nginx`/`nbd`; never break the
car's write path; never ship secrets in artifacts; never `uninstall` while the car
is using the drive.
