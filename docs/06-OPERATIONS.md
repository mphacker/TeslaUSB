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

Live resize takes the affected LUN offline for ~30–60 s (grow)
or several minutes (exFAT shrink — see warning below). The
device must remain on AC power during the operation.

Flow when the web UI's "Apply" button is clicked:

1. Validate `teslacam_gb + media_gb <= sd_total_gb - os_reserve_gb`.
2. Refuse to shrink below `current_used + safety_margin` (worker
   IPC consults the indexer to compute used bytes).
3. `teslausb-present-usb down` — UDC unbind, Tesla loses both
   drives.
4. `systemctl stop nbd-attach@<n> teslafat@<n>` for the affected
   LUN.
5. `truncate -s <new>G` (grow) or full image-copy (shrink).
6. Filesystem resize (`fsck.exfat` / `mkfs.exfat` rebuild for
   shrink, `truncate` only for grow on exFAT — Tesla re-formats
   on first sight).
7. `systemctl start teslafat@<n> nbd-attach@<n>`.
8. `teslausb-present-usb up` — UDC re-bind, Tesla sees both
   drives.

The helper is invoked through a narrow sudoers fragment
(`/etc/sudoers.d/teslausb-resize`) that allowlists exactly this
script. Validated with `visudo -c -f` at install time.

> **exFAT shrink is destructive.** No native in-place shrinker
> exists. The helper copies the live clip set into a smaller
> fresh image; anything not yet captured by the indexer is
> lost. The web UI shows a loud warning before allowing shrink.

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
