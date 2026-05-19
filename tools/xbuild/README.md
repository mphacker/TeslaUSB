# `tools/xbuild/` — cross-compile Rust to aarch64 for the Pi

The Pi Zero 2 W (464 MB RAM, single SDIO bus) cannot host the Rust
toolchain. Two attempts during Phase H1 to `cargo build` on-device
both wedged the device:

- First attempt — cold `cargo build --release -p teslafat` (default
  `-j 4`): rustc peak RSS exceeded available RAM, swap thrashed the
  shared SDIO controller, kernel starved the WiFi watchdog feeder,
  hard power cycle required.
- Second attempt — `cargo build` (debug, `-j 1`, `nice -n 19`,
  `nohup`-detached): wedged systemd PID 1 dbus mid-build. `systemctl`
  / `loginctl` / `systemctl restart systemd-logind` / `/sbin/reboot`
  all failed with `Connection timed out` / `Transport endpoint is
  not connected`. Recovered only via `echo b > /proc/sysrq-trigger`.

See `docs/adr/0008-cross-compile-only.md` for the codified decision.
This directory is the canonical way every Rust binary destined for
the Pi gets built.

## Prerequisites

- Podman (Docker also works — substitute `docker` everywhere below)
- A Podman machine running, if on Windows / macOS:
  `podman machine init && podman machine start`

## One-time setup

Build the image (the first build takes ~5 minutes — it downloads the
base image, the aarch64 cross-toolchain `apt` packages, and the
pinned Rust 1.85.0 toolchain + aarch64 target):

```powershell
podman build -t teslausb-xbuild:latest "$PSScriptRoot"
# or, from the repo root:
podman build -t teslausb-xbuild:latest tools/xbuild
```

Create the three named volumes that keep the cargo cache, git deps,
and `target/` dir warm between runs (without these, every build is a
~5-minute cold compile):

```powershell
podman volume create teslausb-cargo-cache
podman volume create teslausb-cargo-git
podman volume create teslausb-target-aarch64
```

## Build the daemon

From the repository root:

```powershell
$repo = (Resolve-Path .).Path
podman run --rm `
    -v "${repo}/rust:/work:Z" `
    -v teslausb-cargo-cache:/usr/local/cargo/registry `
    -v teslausb-cargo-git:/usr/local/cargo/git `
    -v teslausb-target-aarch64:/work/target `
    teslausb-xbuild:latest `
    bash -c "cargo build --release --target=aarch64-unknown-linux-gnu -p teslafat"
```

(On Linux / macOS the same command works without the backticks; drop
the `:Z` SELinux relabel suffix if you are not on Fedora-family.)

Warm builds complete in ~12 seconds on a modern dev box. The binary
lands inside the named volume at
`/work/target/aarch64-unknown-linux-gnu/release/teslafat`.

## Extract the binary

```powershell
$out = Join-Path $env:TEMP 'teslausb-h1'
New-Item -ItemType Directory -Path $out -Force | Out-Null
podman run --rm `
    -v teslausb-target-aarch64:/work/target `
    -v "${out}:/out:Z" `
    teslausb-xbuild:latest `
    bash -c "aarch64-linux-gnu-strip --strip-unneeded /work/target/aarch64-unknown-linux-gnu/release/teslafat -o /out/teslafat"
"sha256:"
(Get-FileHash -Algorithm SHA256 "$out\teslafat").Hash
```

The stripped binary is ~1.7 MB. Deploy with:

```powershell
scp "$out\teslafat" pi@cybertruckusb.local:/tmp/teslafat
ssh pi@cybertruckusb.local 'sudo install -m 0755 /tmp/teslafat /usr/local/bin/teslafat'
```

## What NOT to do

- **Do not** `apt install rustup` or `apt install build-essential` on
  the Pi. The Pi must never own a Rust toolchain.
- **Do not** `cargo install` anything on the Pi. Same reason.
- **Do not** add a `cargo` step to `setup.sh` (`docs/00-PLAN.md`
  Phase 6 row 6.1 explicitly forbids this).
- **Do not** drop the named volumes (`teslausb-cargo-cache`,
  `teslausb-cargo-git`, `teslausb-target-aarch64`). Without them
  every build is a cold compile.

## Updating the image

When `rust/rust-toolchain.toml` bumps the channel, edit the version
in `Dockerfile` (search for `1.85.0`) and rebuild:

```powershell
podman build --no-cache -t teslausb-xbuild:latest tools/xbuild
```

The named volumes survive image rebuilds; cargo will just notice
the new compiler version and recompile the deps on the next run.
