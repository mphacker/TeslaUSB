# ADR-0022 — WiFi stability stack (BCM43436 SDIO)

**Status:** Accepted, hardware-verified 2026-05-28 on `cybertruckusb.local`.
**Related:** ADR-0021 (disk-backed spill — same "never drop user data"
charter clause), `docs/06-OPERATIONS.md` "WiFi stability stack",
`setup-lib/12-wifi-stability.sh`.

## Context

The Pi Zero 2 W ships with the on-board Cypress BCM43436 SDIO WiFi
chip. Under sustained TX (typical trigger: a cloud sync running
`rclone` for more than a few minutes), the chip firmware deadlocks.
The host kernel keeps running so the hardware watchdog (`bcm2835_wdt`)
never trips, but `wlan0` stops passing packets entirely.

Reproducible signatures in `dmesg` at lockup:

```
brcmfmac: brcmf_sdio_bus_rxctl: resumed on timeout
brcmfmac: brcmf_proto_bcdc_query_dcmd: brcmf_proto_bcdc_msg failed w/status -110
brcmfmac: ... HT Avail request error: -5
```

Once wedged, the only known recoveries are:

1. `nmcli radio wifi off && modprobe -r brcmfmac && modprobe brcmfmac && nmcli radio wifi on`, OR
2. a full reboot.

Pre-stack behaviour: the v1-era "3-min-no-ping ⇒ reboot" watchdog
fired roughly every 30 minutes whenever cloud sync was active. The
device rebooted mid-clip, mid-upload, mid-anything. The operator
directive *"the device must never fully lose wifi or SSH
capabilities"* makes "just let it reboot" unacceptable as a primary
strategy, but it remains acceptable as a last-resort fallback.

## Decision

Adopt a **five-pillar layered stack** that keeps the device on the
air at full upload throughput, recovers from lockups with the
softest possible action first, and reboots only as a true last
resort.

### Pillar 1 — Driver hardening (`setup-lib/12-wifi-stability.sh`)

- NetworkManager drop-in `wifi.powersave = 2` (off) globally.
  PS_POLL is the reproducible trigger for the firmware lockup; we
  trade a few mA of idle current for orders-of-magnitude
  reliability.
- `modprobe.d/brcmfmac.conf`:
  - `roamoff=1` — disable in-driver roaming, which races our
    captive-portal AP-fallback dispatcher (`setup-lib/05-network.sh`).
  - `feature_disable=0x82000` — disable BRCMF_FEAT_PNO (bit 13) and
    BRCMF_FEAT_SCAN_V2 (bit 19). Both run offloaded on the chip and
    have been observed to silently wedge the firmware under load.

### Pillar 2 — `wifi-watchdog` graduated recovery

A 30 s `systemd.timer` runs `/usr/local/sbin/wifi-watchdog.sh`,
which pings the default gateway. On consecutive failures it walks
an escalation ladder (see `docs/06-OPERATIONS.md` for the table).
**Heavy actions fire only on an exact fail-count match** so a
recovery in progress does not re-trigger the same action. Tiers:

- **2** — set `/run/teslausb/uploads_paused` + `nmcli device down/up`.
- **4** — `brcmfmac` SDIO unbind/bind (resets firmware without
  unloading the module; falls back to `modprobe -r/modprobe` if
  the `/sys/bus/sdio/drivers/brcmfmac/<mmcX:YYYY:Z>` node is not
  found). NM connection is brought down first then back up so the
  device is idle during the reset. `modprobe -r brcmfmac` alone
  fails with "Module is in use" when NM still holds the device —
  this exact failure caused the 2026-05-28 14:50 tier-10 reboot.
- **6** — `ip link set wlan0 down/up`.
- **10** — `reboot`.

After **2 consecutive healthy ticks**, the pause flag is removed.
State lives in `/run/teslausb/wifi-watchdog.state`; concurrency
guarded by `flock` on `/run/teslausb/wifi-watchdog.lock`.

### Pillar 3 — Polite `rclone` + uploader cool-down

- `cloud_rclone_service._build_transfer_command` appends
  `--transfers 1 --checkers 1 --tpslimit 4 --buffer-size 4M
  --use-mmap --low-level-retries 3` after the existing
  `--bwlimit` logic. We want one polite stream, not aggressive
  parallelism that hammers the SDIO bus.
- `cloud_rclone_service.Popen` uses
  `preexec_fn=_lower_rclone_priority` which calls `os.nice(19)` +
  raw `ioprio_set(2, IOPRIO_CLASS_IDLE, 7)` via `ctypes` in the
  forked child. We chose `preexec_fn` over prepending
  `nice`/`ionice` to the command list because
  `test_transfer_copy_builds_expected_command` asserts
  `command[0] == str(binary_path)` and a wrapper prefix would
  break it.
- `cloud_archive.uploader._drain_once` checks
  `/run/teslausb/uploads_paused` at the top of every candidate
  loop; if present, sleeps 15 s and `continue`s. Between every
  two successful uploads it also waits 1.5 s
  (`INTER_FILE_COOLDOWN_SECONDS`) so the chip catches its breath.

### Pillar 4 — `teslausb-web.service` resource caps

In `setup-lib/04-units.sh`'s `B1_WEB_UNIT_BODY` heredoc:

```ini
MemoryHigh=300M
MemoryMax=400M
OOMPolicy=stop
TasksMax=128
CPUWeight=80
IOWeight=80
```

`OOMPolicy=stop` is **deliberate, not conservative**. The
operator's standing directive is *"any critical OOM does reboot
the device. It is critical that the device never fully loses wifi
or SSH capabilities."* If the web tier ever runs the box out of
memory, we want it killed and the system watchdog to reboot —
**not** for the OOM killer to start picking sshd or
NetworkManager.

### Pillar 5 — Reboot remains the last-resort safety net

We did not weaken the system watchdog. `watchdog.service`
(`setup-lib/07-watchdog.sh`) still fires on a kernel hang;
`wifi-watchdog` tier 10 still reboots on a sustained WiFi
failure that the soft tiers could not recover from. The whole
stack is a *strategy* for reducing reboot frequency from ~30 min
to ~never, not for *forbidding* reboots.

## Consequences

### Positive

- Cloud sync can run for hours without wedging WiFi.
- Web UI and SSH stay responsive during heavy upload because the
  rclone subprocess runs at nice 19 + IDLE I/O class.
- Lockups that *do* happen recover in 60–90 s via brcmfmac
  reload rather than the 90+ s reboot.
- All five pillars are independently testable and individually
  reversible (each artifact has a `.b1-backup-<ts>` sibling).

### Negative / accepted tradeoffs

- Sustained upload throughput is intentionally capped (`--transfers 1
  --tpslimit 4`). On a high-bandwidth tethered hotspot this leaves
  performance on the table; for the real-world LTE/hotspot target
  it is irrelevant.
- `iw` was added to `setup-lib/01-packages.sh` runtime packages
  (previously only referenced in sudoers and `05-network.sh`).
- `feature_disable=0x82000` masks two firmware features we'd
  otherwise benefit from (offloaded scan, PNO). The trade is
  worth it on this specific chip; future hardware should
  reassess.
- Files under `deploy/wifi-stability/` MUST stay LF
  (`.gitattributes` enforces). A Windows `scp` of a CRLF working
  copy will silently break `brcmfmac.conf` (libkmod warns and
  ignores the line) and the `wifi-watchdog.sh` shebang
  (`env: 'bash\r': No such file or directory`). The setup script
  installs from a Pi-side git checkout where autocrlf is off, so
  this is a manual-deployment-only hazard.

### Deferred (not in this ADR)

The original 5-pillar plan also included **adaptive throttle**
(probe latency and back off TX rate), **request single-flight**
on the web UI's status endpoint, **status caching**, and **HTTP
client backoff in the dashboard JS**. Those are tracked as
session todos (`s2-adaptive`, `s4-singleflight`,
`s4-statuscache`, `s4-backoff`) and deferred until the current
stack proves stable in production.

## Verification

Hardware-verified 2026-05-28 14:30 EDT on `cybertruckusb.local`:

- `cat /sys/module/brcmfmac/parameters/roamoff` → `1`
- No `unknown parameter` warnings in `dmesg` for `feature_disable`.
- `iw dev wlan0 get power_save` → `Power save: off`.
- `wifi-watchdog.timer` ticking every 30 s; service emits
  `wifi-watchdog healthy (gw=10.0.0.1 fail=0 healthy_ticks=N)`.
- `systemctl show teslausb-web.service -p MemoryMax,OOMPolicy,TasksMax`
  → `MemoryMax=419430400 OOMPolicy=stop TasksMax=128`.

One dead-man reboot fired during deploy when the manual
`brcmfmac` reload sequence wedged WiFi — exactly what the
dead-man is for; the device came back clean with all module
options live. The boot-time modprobe (not the live reload) is
the canonical path for applying these options after install.
