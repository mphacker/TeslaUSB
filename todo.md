# Future Enhancements

## User Experience
- [ ] Tighten small-screen ergonomics on Videos/Chimes/Light Shows (stack session grid to 1x6 on narrow viewports, sticky controls at bottom, avoid horizontal scroll in tables, and keep the hamburger/theme toggles reachable during playback/long lists).
- [ ] Show clearer mode-switch progress (unbind, unmount, remount, Samba restart) with success/error toasts and retry guidance.
- [ ] Provide filtering/search in Videos, Chimes, and Light Shows to quickly find items in large libraries.
- [ ] Add per-item status badges (read-only, pending conversion, invalid format) and inline remediation tips.

## New Functionality
- [X] Offline fallback access point: start hostapd/dnsmasq with static 192.168.4.1 when STA join fails (with RSSI/dwell hysteresis to avoid flaps) so a phone can reach the web UI in the car.
- [ ] Optional cloud/off-Pi backup target (S3/Backblaze/SMB) with bandwidth caps and scheduled sync windows.
- [ ] Scheduling UI to auto-switch between Present/Edit based on time-of-day or WiFi presence to avoid manual toggles.
- [ ] Web UI control to regenerate/resize the USB images safely (with pre-flight free-space checks and fsck).
- [ ] Event log UI (connect/disconnect, mode switches, uploads/deletes, errors) with export to CSV/JSON.
- [ ] Support for OTA/lightweight upgrades that download and stage new releases, then prompt for a controlled restart.

## Security
- [ ] Add authentication for the web UI (per-user credentials or OAuth device flow) with session timeouts and CSRF protection.
- [ ] Serve the UI over HTTPS (self-signed by default with option to upload a certificate) and HSTS when enabled.
- [ ] Validate and limit upload types/sizes server-side for all endpoints; quarantine and log rejected files.
- [ ] Harden sudo/system command usage by tightening allowed commands in sudoers and auditing subprocess calls.
- [ ] Add security audit checks (open ports, Samba share settings, world-writable paths) with remediation guidance.

## Reliability
- [ ] Systemd watchdog/healthcheck for `gadget_web.service` and thumbnail generator to auto-restart on hangs.
- [ ] Automated pre/post mode-switch diagnostics: verify nsenter mounts, loop devices, Samba state, and bail out with clear errors.
- [ ] Periodic fsck of both images while in Edit mode with snapshot/loop isolation and alert on corruption trends.
- [ ] Metrics endpoint (Prometheus/text) exposing mode durations, mount failures, thumbnail latency, and Samba reload counts.
- [ ] Integration tests for mode switching and file operations (Videos/Chimes/Light Shows) running in CI to catch regressions.

## Just for Fun
- [ ] Optional Tesla-themed dashboard skin with live car silhouette animations during mode switches.
- [ ] Light show/chime “marketplace” section to browse curated community packs with one-click download to part2.
- [ ] Animated thumbnail scrubbing (GIF-style previews) for recent dashcam clips.
- [ ] Fun ambient “charging” sound or LED blink pattern on the Pi while thumbnails are generating or during long syncs.
- [ ] Easter-egg Konami code in the UI to trigger a celebratory light show demo.
