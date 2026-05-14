# Operator Documentation

Welcome — this section is for **operators**, the people who deploy,
configure, and run TeslaUSB devices. Whether you have one device
plugged into your own car or you're managing a fleet, you should be
able to answer every "what does this thing do, and how do I make it
do what I want?" question by reading documents in this folder.

If you're here to **modify the source code**, you want
[`../contributor/README.md`](../contributor/README.md) instead.

If you just installed your device for the first time, the
[main project readme](../../readme.md) is the most up-to-date
quickstart — come back here once the web interface is up and you
want to know what each page does and how to keep things healthy.

---

## How to use this section

Read the [top-level documentation README](../README.md) first if you
haven't — it explains the conventions used throughout (Mermaid
diagrams, source-file citations, etc.).

Then dip in based on what you need:

| If you want to…                                              | Start with                                                            |
|--------------------------------------------------------------|-----------------------------------------------------------------------|
| Understand what the device does end-to-end                   | [`../VIDEO_LIFECYCLE.md`](../VIDEO_LIFECYCLE.md)                       |
| Understand how the parts fit together                        | [`../ARCHITECTURE.md`](../ARCHITECTURE.md)                             |
| Look up an unfamiliar term                                   | [`../GLOSSARY.md`](../GLOSSARY.md)                                     |
| Adjust a configuration value safely                          | `operator/CONFIGURATION_REFERENCE.md` *(planned, Wave 9)*              |
| Find which systemd service does what                         | `operator/SERVICES_AND_TIMERS.md` *(planned, Wave 9)*                  |
| Set up a cloud provider for backup                           | `operator/CLOUD_PROVIDERS_SETUP.md` *(planned, Wave 9)*                |
| Diagnose a Failed Jobs entry                                 | `operator/FAILED_JOBS_AND_HEALTH.md` *(planned, Wave 9)*               |
| Recover after a power loss / crash                           | `operator/BACKUP_AND_RECOVERY.md` *(planned, Wave 6)*                  |
| Troubleshoot a non-obvious failure                           | `operator/TROUBLESHOOTING.md` *(planned, Wave 10)*                     |

Documents marked *planned* are scheduled for an upcoming
documentation wave (see the
[status table](../README.md#documentation-status)). Until they land,
the most authoritative source is the source code itself plus
[`.github/copilot-instructions.md`](../../.github/copilot-instructions.md).

---

## What you should know up front

A few facts that every operator hits on day one:

1. **The web interface runs on port 80**, not 5000. URLs are simply
   `http://<pi-ip>` or `http://<hostname>.local`. Port 80 is required
   for the captive-portal splash screen to fire automatically when you
   join the device's WiFi.

2. **There is no "Edit Mode" / "Present Mode" toggle in the UI.**
   The device handles RO/RW transitions transparently via
   `quick_edit_part2`. The status dot in the header tells you the
   visible state: green = normal, amber = "Network Sharing Active"
   (Samba is up). All write operations work from the UI without you
   needing to switch modes manually.

3. **`*.img` files are protected.** Every delete code path refuses
   to delete an `.img` file in `installation.mount_dir`. If you really
   need to remove one, do it from a shell.

4. **Tesla recording is sacred.** The device's #1 priority is to
   present the USB drive within ~3 seconds of boot so Tesla never
   misses a frame. Every background subsystem (archive, indexer,
   cloud sync, LES) is read-only and yields to Tesla writes.

5. **Reboots that look like crashes might be normal vehicle sleep.**
   Tesla cuts USB power when the car sleeps. Unclean-shutdown journal
   files (`*.journal~`) accumulate from both real watchdog resets and
   normal vehicle sleep. The
   [`MEMORY_AND_WATCHDOG.md`](../contributor/subsystems/MEMORY_AND_WATCHDOG.md)
   *(planned, Wave 6)* doc explains how to tell them apart.

---

## Where to look when something is wrong

Until the dedicated `TROUBLESHOOTING.md` lands, here is the
short version:

- **Check `/jobs`** — the Failed Jobs page surfaces every stuck
  queue row across all subsystems with a recommendation
  (Retry / Delete / Either) and a clip-value badge so you know
  what's at stake.
- **Check `/api/system/health`** — JSON dump of disk, memory,
  worker liveness, queue depths, retention summary, archive
  watchdog severity.
- **Check `journalctl -u gadget_web.service -f`** for live logs.
- **Check the `.github/copilot-instructions.md` file** for the
  "Pitfalls to avoid" section, which lists known failure-mode
  signatures.

---

## Source files

This document is purely navigational; it has no source-code
dependency.
