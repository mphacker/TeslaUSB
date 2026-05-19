# ADR-0008 — Cross-compile Rust binaries on the dev box; never build on the Pi

| Status   | Accepted |
|----------|----------|
| Date     | 2026-05-19 |
| Deciders | mphacker (operator), Copilot CLI (charter enforcement) |
| Phase    | Phase H1 of B-1 rewrite |
| Crashes  | Two H1 attempts wedged the device (one hard power cycle, one sysrq+b reboot) |

## Context

Phase H1 is the first deploy of a Rust binary to
`cybertruckusb.local` (Raspberry Pi Zero 2 W, 464 MB RAM, kernel
`6.12.47+rpt-rpi-v8 aarch64`). The original `docs/00-PLAN.md` row
H1.1 wording left it open to the operator whether the binary would
be cross-compiled on the dev box or built directly on the Pi —
*"cross-build on dev box (or build on-device — slower but simpler
for skeleton)."*

We tried "build on-device" twice. Both attempts wedged the device.

### Crash #1 — cold release build, `-j 4` (default parallelism)

```bash
ssh pi@cybertruckusb.local
cd ~/teslausb-b1/rust
~/.cargo/bin/cargo build --release -p teslafat
```

The release build hit `rustc` peak resident-set sizes well above the
464 MB physical RAM ceiling. The kernel pushed `rustc` into swap on
the same SDIO bus that the WiFi chip uses for SDIO transfers and
that the SD-card root filesystem uses for journal commits. Within
~10 minutes:

- mDNS resolution stopped responding (`ping cybertruckusb.local` →
  `Could not find host`)
- The arp cache entry for the device went stale; even direct-IP
  pings stopped getting replies
- SSH `kex_exchange_identification: write: Connection reset by peer`
  on every reconnect attempt
- WiFi link went down (the watchdog feeder thread was starved out
  by swap I/O contention)
- No further forward progress from the device; only resolution was
  a hard power cycle (pull the USB-C cable, wait, plug back in)

The build had reached "Compiling tokio v1.47.1" before the device
became unreachable. Cold rustc peak RSS for a single tokio compile
unit is ~250-300 MB, which overlapped with the cargo driver process
+ the kernel + sshd + systemd + the WiFi firmware buffer; total
working set exceeded RAM and the system was committed to swap to
make any forward progress at all.

### Crash #2 — debug build, `-j 1`, `nice -n 19`, `nohup`-detached

To avoid the parallelism + release-profile pressure of crash #1:

```bash
ssh pi@cybertruckusb.local
cd ~/teslausb-b1/rust
nohup nice -n 19 ~/.cargo/bin/cargo build --jobs 1 -p teslafat > /tmp/build.log 2>&1 &
disown
```

The build progressed further (~13 min in, mid-dependency-graph),
but a different failure mode emerged. Every new SSH connection
authenticated successfully (`pam_unix` works at the kernel level),
but `pam_systemd` hung indefinitely registering the session:

```
sshd-session[1784]: pam_systemd(sshd:session): Failed to create session: Connection timed out
```

`pam_systemd` is a userspace shim that talks to
`systemd-logind` over D-Bus. `loginctl list-sessions` reported
*"No sessions."* — falsely, since there WERE active sessions —
because `systemd-logind` itself could not be queried:

```
$ systemctl status systemd-logind
Failed to get properties: Connection timed out

$ sudo systemctl restart systemd-logind
Failed to restart systemd-logind.service: Transport endpoint is not connected

$ sudo systemctl is-system-running
Failed to query system state: Transport endpoint is not connected
```

systemd PID 1's D-Bus event loop was deadlocked. The classic
symptom of memory pressure on a systemd-based system whose
`init` itself ran out of working set under swap thrash. Once
PID 1's D-Bus is wedged the only fixes are:

- `sudo /sbin/reboot` — itself a D-Bus call to PID 1, fails with
  `Call to Reboot failed: Connection timed out`
- `echo b > /proc/sysrq-trigger` — kernel-level reboot, bypasses
  systemd entirely

The recovery in crash #2 took sysrq+b. Sysrq was not enabled by
default (`/proc/sys/kernel/sysrq` = `438` = "no SAK, no reboot");
had to `echo 1 > /proc/sys/kernel/sysrq` first, then `echo b`.
Total recovery time from "build looks fine" to "device back at
prompt" was ~25 minutes of frustrated diagnosis.

### Both crashes are structural to the platform

Crash #1 and crash #2 are not a bug in the build settings; they are
a consequence of the Pi Zero 2 W's single SDIO bus shared between
WiFi + SD card + kernel paging. Any workload that:

- has working set > available RAM (~350 MB usable after kernel +
  systemd + sshd + WiFi firmware), AND
- forces the kernel to page-out under load,

will saturate that bus and cause either a WiFi watchdog reset or a
systemd D-Bus deadlock. The Rust compiler crosses both thresholds
trivially — even `-j 1 nice -n 19` debug is too heavy.

This is the same failure class that took down v1 in production
multiple times (`docs/01-PROGRESS.md` Phase H0 row H0.13 records a
similar D-Bus wedge during decommissioning that needed a power
cycle). The Pi simply cannot host a compiler.

## Decision

§A — **Every Rust binary destined for `cybertruckusb.local` is
cross-compiled from a dev-box environment.** Building Rust on the
Pi is forbidden.

§B — **The canonical cross-compile environment is the Podman image
defined in `tools/xbuild/`**, pinned to the same toolchain version
that `rust/rust-toolchain.toml` declares. Operator runbook lives in
`tools/xbuild/README.md`. This is the only supported path that is
reproducible across operator machines.

§C — **`setup.sh` (Phase 6) MUST NOT install `rustup`, `cargo`,
`gcc`, `build-essential`, or kernel headers for compiling Rust** on
the Pi. The Pi's package surface should not contain a compiler at
all. (Phase 6 row 6.1 in `docs/00-PLAN.md` is amended accordingly.)

§D — **The H1 hardware checklist is amended** to require deploying
a cross-compiled binary from `$env:TEMP/teslausb-h1/teslafat` (or
the operator's equivalent extracted artifact directory) and to
forbid any `cargo build` invocation on the Pi.

§E — **If a future maintainer believes they need a compiler on the
Pi**, they MUST first write a new ADR that supersedes this one,
documenting the new platform constraints (e.g., Pi 5 swap to a
8 GB SBC with SSD storage where the failure mode genuinely no
longer exists) and the experimental evidence that compilation no
longer wedges the device. Until that ADR exists, the prohibition
stands.

## Consequences

**Positive:**

- Cross-builds complete in ~12 s warm (vs. ~25 min on-device for
  the same skeleton, when the device doesn't crash).
- Build is reproducible regardless of Pi state — `setup.sh` testing
  on a freshly-flashed SD card does not require building anything;
  the operator just `scp`s a pre-built binary.
- The Pi's package surface stays small (no `rustup`, no `gcc`, no
  kernel headers eating ~500 MB of SD card and ~100 MB of
  unmaintained attack surface).
- The Pi never enters a swap-thrash failure mode caused by the
  build, isolating "deployment failed" from "device wedged".

**Negative:**

- Adds a dev-box prerequisite (Podman or Docker + ~5 minutes for
  the first image build).
- Cross-compile correctness must be verified per platform; the
  Phase 1 ZeroBackend + future Phase 2 FAT synthesizer have no
  platform-specific syscalls, but Phase 5 (worker, IPC, sd-systemd)
  may need careful attention to glibc version skew. (Mitigation:
  the image's `rust:1-bookworm` base matches the Pi's
  `bookworm`-era glibc, so symbol versions resolve cleanly.)

**Trade-offs accepted:**

- Operators on Apple Silicon must run the build through Podman's
  qemu-user emulation layer; the warm-build cost rises from ~12 s
  to ~45 s. Tolerable.
- We do not (yet) ship a Linux-native cross-build script as an
  alternative to Podman. If a future operator needs one, follow
  the same toolchain pinning pattern in the Dockerfile.

## Alternatives considered

**A. Build on a swap-heavy Pi 5 reference unit instead.** Rejected:
B-1's target hardware is the Pi Zero 2 W. Building elsewhere just
to deploy here is a more expensive version of "cross-compile" with
extra failure modes.

**B. Use `cross` (the cargo subcommand) instead of a raw Podman
image.** Considered. `cross` would work, but adds a dependency
chain (Rust → cargo → cross → docker/podman → image) that obscures
the toolchain pin. The bespoke Dockerfile is ~30 lines and pins
the toolchain explicitly. We can revisit if `cross` ever becomes
necessary for a multi-target build matrix.

**C. Use GitHub Actions to build the binary and download it.**
Rejected per operator's standing preference (2026-05-19): "prefer
to not rely on github actions for now." The Podman path keeps the
build entirely on the operator's machine.

**D. Cross-compile from the Windows host with `rustup target add
aarch64-unknown-linux-gnu`.** Considered. Works for `--target` but
the linker is missing on Windows (`aarch64-linux-gnu-gcc` is not
available); installing the Linaro toolchain on Windows adds
PATH-management complexity. Podman normalises the dev-host's OS
to "irrelevant" — the same Dockerfile + named volumes work on
Windows, Linux, and macOS.

## References

- Phase H1 crash transcripts in session checkpoint
  `044-h1-deploy-complete.md` (next checkpoint after this ADR).
- `tools/xbuild/Dockerfile` + `tools/xbuild/README.md` — canonical
  cross-build environment + operator runbook.
- `docs/00-PLAN.md` Phase H1 row H1.1 (forbids on-device build,
  references this ADR).
- `docs/00-PLAN.md` Phase 6 row 6.1 (forbids installing `rustup`
  on the Pi, references this ADR).
- `rust/rust-toolchain.toml` line 14 — `channel = "1.85.0"` (the
  pin that the Dockerfile mirrors).
