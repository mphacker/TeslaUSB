# Future Enhancements

## Security (Priority: Critical)
- [ ] **CSRF Protection**: Add Flask-WTF with CSRF tokens on all POST forms to prevent cross-site request forgery attacks
- [ ] **Input Sanitization & Path Traversal Protection**: Use `werkzeug.security.safe_join()` for all file path operations to prevent directory traversal attacks
- [ ] **Secure Secret Key Persistence**: Generate SECRET_KEY once and save to `.env` file instead of regenerating on every restart (loses sessions)
- [ ] **Rate Limiting**: Add Flask-Limiter to prevent abuse on file operations, uploads, deletes, and mode switches (e.g., "10 per minute")
- [ ] Add authentication for the web UI (HTTP Basic Auth or Flask-Login with single admin user, or OAuth device flow) with session timeouts
- [ ] Serve the UI over HTTPS (self-signed by default with option to upload a certificate) and HSTS when enabled
- [ ] Validate and limit upload types/sizes server-side for all endpoints; quarantine and log rejected files
- [ ] Harden sudo/system command usage by tightening allowed commands in sudoers and auditing subprocess calls
- [ ] Add security audit checks (open ports, Samba share settings, world-writable paths) with remediation guidance

## Performance & Memory Optimization (Priority: High)
- [ ] **Lazy Load Video Thumbnails**: Implement intersection observer to load thumbnails as they scroll into view (3-5x faster page loads)
- [ ] **Paginate Video Lists**: Add pagination (50 videos per page) with "Load More" button to handle 500+ video libraries
- [ ] **Cache Partition Mount Paths**: Cache `get_mount_path()` results in memory for 30 seconds to reduce I/O operations
- [ ] **Optimize Video Statistics**: Cache `get_video_statistics()` results for 5 minutes instead of scanning entire tree on every analytics page load
- [ ] **Background Task Queue**: Use Python threading or lightweight task queue for long operations (thumbnail generation, bulk deletes) to avoid blocking web requests

## User Experience (Priority: High)
- [ ] Tighten small-screen ergonomics on Videos/Chimes/Light Shows (stack session grid to 1x6 on narrow viewports, sticky controls at bottom, avoid horizontal scroll in tables, and keep the hamburger/theme toggles reachable during playback/long lists)
- [ ] **Bulk Video Selection**: Add checkboxes and "Delete Selected" button for managing specific videos (alternative to "all or nothing")
- [ ] **Video Search/Filter**: Add search box with live filtering by filename, date, camera, or session ID for large libraries
- [ ] Show clearer mode-switch progress (unbind, unmount, remount, Samba restart) with success/error toasts and retry guidance
- [ ] **Progress Indicators**: Add progress bars using JavaScript + Server-Sent Events or polling for file uploads, bulk deletes, mode switches
- [ ] **Toast Notifications**: Replace flash messages with modern JavaScript toast library for non-blocking success/error notifications
- [ ] **Keyboard Shortcuts**: Add power-user shortcuts (`?`=help, `p`=present mode, `e`=edit mode, `v`=videos, `c`=chimes)
- [ ] Provide filtering/search in Chimes and Light Shows to quickly find items in large libraries
- [ ] Add per-item status badges (read-only, pending conversion, invalid format) and inline remediation tips
- [ ] **Chime Preview Before Upload**: Add client-side audio preview in upload form to avoid uploading wrong files
- [ ] **Quick Actions Menu**: Add floating action button (FAB) with quick access to upload, mode switch, refresh
- [ ] **Better Error Messages**: Replace generic "Error: ..." with specific, actionable messages with troubleshooting hints
- [ ] **Confirmation Modals**: Replace ugly JavaScript `confirm()` with custom modal dialogs
- [ ] **Empty State Messages**: Add helpful first-run messages like "Your Tesla hasn't recorded any videos yet. Try honking to save a clip!"
- [ ] **Loading Skeletons**: Add CSS skeleton loaders instead of blank pages while data loads
- [ ] **Favicon & App Icons**: Add Tesla/USB themed favicon and touch icons for browser tabs and mobile home screen

## Configuration & Maintenance (Priority: Medium)
- [ ] **Web-Based Configuration Editor**: Add settings page to edit `config.sh` and `config.py` (AP credentials, cleanup policies, port) without SSH
- [ ] **Logging Dashboard**: Add logs page showing recent entries from `journalctl` for all gadget services (no SSH required)
- [ ] **Disk Space Warnings**: Add prominent warning banner when >85% full, critical alert at >95% to prevent recording failures
- [ ] **Health Check Dashboard**: Add status page showing CPU temp, disk I/O, mount health, service status, loop device info
- [ ] **One-Click Updates**: Add "Check for Updates" button that runs `git pull` and restart script (with warnings about personal project nature)
- [ ] Web UI control to regenerate/resize the USB images safely (with pre-flight free-space checks and fsck)
- [ ] Event log UI (connect/disconnect, mode switches, uploads/deletes, errors) with export to CSV/JSON

## New Functionality (Priority: Medium)
- [X] Offline fallback access point: start hostapd/dnsmasq with static 192.168.4.1 when STA join fails (with RSSI/dwell hysteresis to avoid flaps) so a phone can reach the web UI in the car
- [ ] Optional cloud/off-Pi backup target (S3/Backblaze/SMB/rsync) with bandwidth caps and scheduled sync windows for important saved clips
- [ ] Scheduling UI to auto-switch between Present/Edit based on time-of-day or WiFi presence to avoid manual toggles
- [ ] Support for OTA/lightweight upgrades that download and stage new releases, then prompt for a controlled restart
- [ ] **Video Export/Share**: Add "Share" button that creates temporary public link or exports selected clips
- [ ] **Chime Library Import**: Add "Import from ZIP" feature for bulk chime collection imports
- [ ] **Persistent Theme Preference**: Store theme in cookie or server-side user setting instead of just localStorage (syncs across devices)

## Reliability (Priority: Medium)
- [ ] Systemd watchdog/healthcheck for `gadget_web.service` and thumbnail generator to auto-restart on hangs
- [ ] Automated pre/post mode-switch diagnostics: verify nsenter mounts, loop devices, Samba state, and bail out with clear errors
- [ ] Periodic fsck of both images while in Edit mode with snapshot/loop isolation and alert on corruption trends
- [ ] Metrics endpoint (Prometheus/text) exposing mode durations, mount failures, thumbnail latency, and Samba reload counts
- [ ] Integration tests for mode switching and file operations (Videos/Chimes/Light Shows) running in CI to catch regressions
- [ ] **Graceful Degradation**: Show informative messages and allow partial functionality when partitions unmounted instead of complete failure
- [ ] **Retry Logic**: Add exponential backoff retry for transient failures in mount operations and subprocess calls
- [ ] **Lock File Monitoring UI**: Add lock file status to operations banner with "Force Clear" button (with warnings about stuck operations)

## Analytics & Insights (Priority: Low)
- [ ] **Storage Trend Graphs**: Track daily storage growth and project "days until full" instead of just point-in-time data
- [ ] **Recording Statistics**: Show clips per day, busiest times, camera distribution, sentry vs regular recording patterns
- [ ] **Chime Usage Tracking**: Track chime activation history and show most-used favorites

## Mobile Experience (Priority: Low)
- [ ] **Improved Mobile UI**: Increase touch target sizes and improve spacing for better in-car usage on mobile viewports
- [ ] **Mobile Video Playback Optimization**: Add lower-quality streaming option or adaptive bitrate for smoother in-car video review
- [ ] **Offline PWA Support**: Make app a Progressive Web App with service worker caching to work when WiFi temporarily disconnects

## Just for Fun (Priority: Low)
- [ ] Optional Tesla-themed dashboard skin with live car silhouette animations during mode switches
- [ ] Light show/chime "marketplace" section to browse curated community packs with one-click download to part2
- [ ] Animated thumbnail scrubbing (GIF-style previews) for recent dashcam clips
- [ ] Fun ambient "charging" sound or LED blink pattern on the Pi while thumbnails are generating or during long syncs
- [ ] Easter-egg Konami code in the UI to trigger a celebratory light show demo

---

## 🎯 Quick Wins (Recommended Starting Point)
**Low effort, high impact improvements to tackle first:**

1. **CSRF Protection** - Critical security fix (Flask-WTF)
2. **Input Sanitization** - Critical security fix (safe_join)
3. **Lazy Load Thumbnails** - Massive performance boost (intersection observer)
4. **Bulk Video Selection** - Most requested feature (checkboxes + batch delete)
5. **Disk Space Warning** - Prevents data loss (simple check + banner)
6. **Better Error Messages** - Reduces confusion (improve flash messages)
7. **Video Search/Filter** - Essential for large libraries (client-side JS filter)
8. **Progress Indicators** - Big UX improvement (polling + spinner)
9. **Persistent Secret Key** - Fixes session issues (save to .env file)
10. **Rate Limiting** - Prevents abuse (Flask-Limiter)
