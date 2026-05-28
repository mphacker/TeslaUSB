# 06 — Operations runbook

This document covers operator-facing knobs that live outside the
web UI, plus the safety rails behind the live-resize and
auto-cleanup features introduced by the AC series.

## Shared storage config — `/etc/teslausb/teslausb.toml`

Single source of truth for LUN sizes and TeslaCam auto-cleanup
behaviour. Both the Flask web UI (`/storage`) and the Rust
`teslausb-worker` read this file. The web UI writes it via an
atomic rename; the worker re-reads it on every cleanup tick so
edits take effect within ~5 minutes without a service restart.

```toml
[storage]
# Reserved for OS + system operations. NOT available to either
# LUN. Default 20 GB. Hard minimum 8 GB (enforced; smaller values
# are rejected by load() and the web UI).
os_reserve_gb = 20

# Size reported to Tesla for /dev/sda (TeslaCam, exFAT).
# Must satisfy: teslacam_gb + media_gb <= sd_total_gb - os_reserve_gb.
teslacam_gb = 64

# Size reported to Tesla for /dev/sdb (Music/media, FAT32).
media_gb = 32

[cleanup]
# Auto-cleanup target free-space percentage on the TeslaCam LUN.
# When set to 0, the worker auto-tunes the target from the
# indexer's median clip size (defaults to 5% if too few samples).
# Sweep runs continuously on the worker cleanup tick.
target_free_pct = 5

# 0 = unlimited (SentryClips are never auto-deleted by age).
# When > 0, SentryClips older than this become Tier-C-by-age
# candidates and are deleted before the last-resort SavedClips
# tier kicks in.
sentry_max_age_days = 0

# Prefer to keep RecentClips that have GPS/SEI metadata.
# When true, those clips move from Tier A into Tier B and are
# only deleted after Tier A is exhausted.
preserve_with_gps = true
```

### Editing safely

1. Edit via the web UI when possible (`/storage`). Validation +
   atomic rename are handled for you.
2. To edit by hand:
   ```bash
   sudo cp /etc/teslausb/teslausb.toml /etc/teslausb/teslausb.toml.bak
   sudo nano /etc/teslausb/teslausb.toml
   # Worker picks the new values up on its next cleanup tick
   # (default every 5 min). LUN size changes require a re-bind —
   # see "Resizing LUNs" below.
   ```
3. The hard minimum on `os_reserve_gb` is 8 GB. Anything lower
   is rejected at load time and the worker falls back to the
   in-memory default (20 GB).

## Resizing LUNs (`teslausb-resize-lun`)

Live resize takes the affected LUN offline for ~30–60 s. The
device must remain on AC power during the operation.

Flow when the web UI's "Apply" button is clicked:

1. Validate `teslacam_gb + media_gb + os_reserve_gb ≤ sd_total_gb`.
2. Validate `os_reserve_gb ≥ 8` (hard floor).
3. Refuse to shrink below the current backing-directory usage
   (`du -sb /srv/teslausb/<lun>` must be ≤ requested size).
4. Rewrite `volume_size_gb` in the per-LUN `teslafat-N.toml`.
5. Rewrite the matching key (`teslacam_gb` or `media_gb`) in
   `/etc/teslausb/teslausb.toml`.
6. `teslausb-hide-usb` — UDC unbind, Tesla loses both drives.
7. `systemctl restart teslafat@<N>` — backend re-synthesises
   the FAT/exFAT image at the new size from the existing
   directory tree.
8. `teslausb-present-usb` — UDC re-bind, Tesla sees both
   drives at their new sizes.

The helper is invoked through a narrow sudoers fragment
(`/etc/sudoers.d/teslausb-resize`) that allowlists exactly this
script with NOPASSWD for the `gadget_web` user; nothing else
is granted.

> **Note on exFAT shrink.** The teslafat backend synthesises
> the FAT/exFAT image on demand from the backing directory
> tree — there is no persistent on-disk image file to copy
> or rewrite. As long as the `du`-measured usage of the
> backing dir fits in the new size (step 3), Tesla simply
> sees a smaller volume after the LUN bounce. If usage does
> not fit, the helper refuses with `exit 3` and leaves all
> state untouched.

## Auto-cleanup behaviour (TeslaCam only)

The worker's cleanup loop runs two passes per tick:

1. **Legacy age-based pass** (`cleanup.run_once`): the existing
   per-bucket age rules from `worker.toml`. Unchanged by AC.
2. **Tier-aware sweep** (`cleanup_sweep::sweep_to_target_now`):
   the AC-series addition. Statvfs's the TeslaCam mount; if free
   < `target_free_pct`, deletes oldest-first within the
   following tier order until the target is met or every
   candidate is exhausted:

   | Tier | What | When deleted |
   |------|------|--------------|
   | A | RecentClips with no GPS waypoints and no SEI tesla-data | First |
   | B | RecentClips that DO have GPS waypoints or SEI tesla-data | After A exhausted (only when `preserve_with_gps = true`; otherwise folded into A) |
   | C (age) | SavedClips, plus SentryClips older than `sentry_max_age_days` (if > 0) | After B exhausted |
   | C (last resort) | SavedClips + remaining SentryClips | Only if A+B+C-age exhausted AND target still unmet |

   Per-clip failures are non-fatal. The sweep summary is logged
   at `info` level on every tick.

Cleanup is **never** run on the Media LUN. The worker's
backing-root path is `/srv/teslausb/teslacam` (LUN 0); the Media
LUN at LUN 1 is operator-managed.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `/storage` UI Apply silently reverts | `journalctl -u teslausb-web -n 50` for ApplyError; verify `/etc/teslausb/teslausb.toml` is writable by the web user |
| Sweep never frees enough space | `journalctl -u teslausb-worker -g cleanup_sweep -n 20`. If `target_reached=false` even with last-resort tier, raise `os_reserve_gb` or shrink the LUN |
| Tesla reports "drive needs formatting" after grow | Expected on exFAT grow; either format from the Tesla touchscreen or run `mkfs.exfat` from the Pi |
| Worker keeps logging "storage_config load failed" | The file is malformed or unreadable. Sweep is skipped — restore from `.bak` and `systemctl restart teslausb-worker` |

## WiFi stability stack (BCM43436 SDIO)

The Pi Zero 2 W on-board WiFi chip (BCM43436) firmware deadlocks
under sustained TX (typical trigger: a cloud sync running rclone
for more than a few minutes). The kernel keeps running so the
hardware watchdog never trips, but `wlan0` stops passing packets.
Signatures in `dmesg`: `brcmfmac: ... HT Avail request error: -5`
and `brcmf_proto_bcdc_query_dcmd ... err=-110`.

Before the stack landed, the device rebooted every ~30 min while
v1's blunt "3-min-no-ping ⇒ reboot" watchdog kicked. The stack
keeps the device on the air at full throughput and only reboots
as last resort. See **ADR-0022 — WiFi stability stack** for the
full design rationale.

### What `setup-lib/12-wifi-stability.sh` installs

| File | Purpose |
|------|---------|
| `/etc/NetworkManager/conf.d/10-teslausb-no-powersave.conf` | `[connection] wifi.powersave = 2` (off) — global default for every new NM profile. Stops the chip dropping into PS_POLL where the firmware lockup is reproducible. |
| `/etc/modprobe.d/brcmfmac.conf` | `options brcmfmac roamoff=1 feature_disable=0x82000` — disables in-driver roaming (races our captive-portal dispatcher) and the offloaded PNO/SCAN_V2 engines (silently wedge under load). Takes effect at next module load (next boot, or `nmcli radio wifi off && modprobe -r brcmfmac && modprobe brcmfmac && nmcli radio wifi on`). |
| `/usr/local/sbin/wifi-watchdog.sh` | Escalation-ladder recovery script (see below). |
| `/etc/systemd/system/wifi-watchdog.service` | `Type=oneshot`, `OOMScoreAdjust=-900`, `Nice=-5`, IO realtime — survives memory pressure. |
| `/etc/systemd/system/wifi-watchdog.timer` | 30 s tick (`OnUnitActiveSec=30s`). Enabled + started by `setup-lib/10-activate.sh`. |

Source artifacts live under `deploy/wifi-stability/` for review.

### `wifi-watchdog.sh` escalation ladder

Health check each tick: ping the default gateway from
`ip route get 1.1.1.1`. State file: `/run/teslausb/wifi-watchdog.state`
(format: `<fail_count> <healthy_ticks>`). Heavy actions fire only
on an **exact** fail-count match so recovery passes don't repeat
the action.

| Fail count | Action |
|------------|--------|
| 1 | Soft log — no recovery yet (could be a one-tick blip). |
| **2** | Touch `/run/teslausb/uploads_paused` (uploader's cool-down loop sees it within 1.5 s and yields). `nmcli device disconnect wlan0 && nmcli device connect wlan0`. |
| 3 | Pause held; log only. |
| **4** | `modprobe -r brcmfmac && modprobe brcmfmac` (re-applies `roamoff` and `feature_disable`). |
| 5 | Pause held; log only. |
| **6** | `ip link set wlan0 down && ip link set wlan0 up`. |
| 7–9 | Pause held; log only. |
| **10** | `reboot` (last resort — never gets here if ladder works). |

After **2 consecutive healthy ticks**, the pause flag is removed
and `fail_count` resets to 0. Operators can manually clear:
`sudo rm -f /run/teslausb/uploads_paused`.

### Uploader cooperation (`web/teslausb_web/services/cloud_archive/uploader.py`)

The cloud-archive uploader checks `/run/teslausb/uploads_paused`
at the top of every candidate in `_drain_once`. When present:
sleep `PAUSE_FLAG_BACKOFF_SECONDS` (15 s) and `continue`. Between
every two successful uploads it also waits
`INTER_FILE_COOLDOWN_SECONDS` (1.5 s) so the WiFi chip catches
its breath.

### Polite rclone subprocess (`cloud_rclone_service.py`)

Every `rclone` invocation gets:

- `--transfers 1 --checkers 1 --tpslimit 4 --buffer-size 4M --use-mmap --low-level-retries 3` appended after the bwlimit logic.
- `preexec_fn=_lower_rclone_priority` on the `Popen` — runs
  `os.nice(19)` + raw `ioprio_set(2, IOPRIO_CLASS_IDLE, 7)` via
  ctypes in the forked child. The command list itself is left
  untouched so `test_transfer_copy_builds_expected_command`
  keeps passing.

### `teslausb-web.service` resource caps (`setup-lib/04-units.sh`)

The web/upload unit has hard caps so it can never starve SSH:

```ini
MemoryHigh=300M
MemoryMax=400M
OOMPolicy=stop           # critical OOM ⇒ unit dies, watchdog reboots
TasksMax=128
CPUWeight=80
IOWeight=80
```

`OOMPolicy=stop` is deliberate — per the operator directive
*"any critical OOM does reboot the device. It is critical that
the device never fully loses wifi or SSH capabilities"*, we'd
rather lose the web tier and let the system watchdog reboot
than let a runaway upload thread eat the box.

### Verification after a fresh install

```bash
# Module options actually loaded
cat /sys/module/brcmfmac/parameters/roamoff               # → 1
# (feature_disable is accepted by the kernel but not exposed in sysfs;
#  presence is verified by the ABSENCE of "unknown parameter" in dmesg)
sudo dmesg | grep -i "brcmfmac.*unknown parameter"        # → empty

# NM powersave applied
sudo iw dev wlan0 get power_save                           # → off
nmcli -t -f 802-11-wireless.powersave connection show <profile>  # → 2

# Watchdog ticking
systemctl is-active wifi-watchdog.timer                    # → active
sudo journalctl -u wifi-watchdog.service --since "5 min ago" | grep healthy

# Upload caps live
systemctl show teslausb-web.service -p MemoryMax,OOMPolicy,TasksMax
#   MemoryMax=419430400 OOMPolicy=stop TasksMax=128
```

### Common operator actions

| Action | Command |
|--------|---------|
| Manually pause all uploads (e.g. during a road trip) | `sudo touch /run/teslausb/uploads_paused` |
| Resume | `sudo rm -f /run/teslausb/uploads_paused` |
| Tail the watchdog | `sudo journalctl -u wifi-watchdog.service -f` |
| Force-reload `brcmfmac` (drops WiFi ~15 s — only via console or with a dead-man timer) | `sudo systemd-run --on-active=180 --unit=b1-deadman /sbin/reboot && sudo nohup sh -c 'sleep 2; nmcli radio wifi off; sleep 2; modprobe -r brcmfmac; sleep 2; modprobe brcmfmac; sleep 3; nmcli radio wifi on' >/tmp/reload.log 2>&1 &` |
| Watch uploader cool-down honouring the pause flag | `sudo journalctl -u teslausb-web -g "uploads_paused\|cool-down"` |

> **CRLF gotcha for hand-editing on Windows.** Files under
> `deploy/wifi-stability/` MUST stay LF (`.gitattributes`
> enforces this). If you ever `scp` them from a Windows
> working tree that has been touched by an editor with CRLF
> defaults, the shebang on `wifi-watchdog.sh` will fail to
> resolve (`env: 'bash\r': No such file or directory`) and
> `brcmfmac.conf` will be silently ignored by `libkmod`
> (`ignoring bad line starting with '\r'`). Always install
> via `setup-lib/12-wifi-stability.sh` (it `cp`s from a fresh
> git-checked-out repo on the Pi where autocrlf is off).
