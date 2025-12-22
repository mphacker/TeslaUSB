# Future Enhancements

## Security (Priority: Critical)
- [ ] **CSRF Protection**: Add Flask-WTF with CSRF tokens on all POST forms to prevent cross-site request forgery attacks
- [ ] **Input Sanitization & Path Traversal Protection**: Use `werkzeug.security.safe_join()` for all file path operations to prevent directory traversal attacks (PARTIAL: `os.path.basename()` used extensively but not `safe_join()`)
- [ ] **Secure Secret Key Persistence**: Generate SECRET_KEY once and save to `.env` file instead of regenerating on every restart (loses sessions)
- [ ] **Rate Limiting**: Add Flask-Limiter to prevent abuse on file operations, uploads, deletes, and mode switches (e.g., "10 per minute")
- [ ] Add authentication for the web UI (HTTP Basic Auth or Flask-Login with single admin user, or OAuth device flow) with session timeouts
- [ ] Serve the UI over HTTPS (self-signed by default with option to upload a certificate) and HSTS when enabled
- [X] Validate and limit upload types/sizes server-side for all endpoints; quarantine and log rejected files (DONE: WAV/MP3 validation for chimes, .fseq/.mp3/.wav for light shows, file size checks)
- [ ] Harden sudo/system command usage by tightening allowed commands in sudoers and auditing subprocess calls
- [ ] Add security audit checks (open ports, Samba share settings, world-writable paths) with remediation guidance

## Performance & Memory Optimization (Priority: High)
- [X] **Lazy Load Video Thumbnails**: Implement intersection observer to load thumbnails as they scroll into view (3-5x faster page loads) (DONE: Using HTML `loading="lazy"` attribute on all thumbnail images)
- [X] **Paginate Video Lists**: Add pagination (50 videos per page) with "Load More" button to handle 500+ video libraries (DONE: Infinite scroll loads 15 initial, then 15 more at 85% scroll)
- [ ] **Cache Partition Mount Paths**: Cache `get_mount_path()` results in memory for 30 seconds to reduce I/O operations
- [ ] **Optimize Video Statistics**: Cache `get_video_statistics()` results for 5 minutes instead of scanning entire tree on every analytics page load
- [X] **Background Task Queue**: Use Python threading or lightweight task queue for long operations (thumbnail generation, bulk deletes) to avoid blocking web requests (DONE: systemd timer runs thumbnail generator every 15 min, thumbnails generated async)
- [ ] Cache analytics responses (partition/video stats) with a short TTL and background refresh to avoid full rescans on every dashboard/API request
- [ ] Add server-side pagination/indexed listings so video pages don’t scan entire folders before returning the first page
- [ ] Cache per-folder session indexes for multi-camera view to avoid re-parsing entire folders per session request
- [ ] Limit PyAV decode threads to 1 and cap concurrent thumbnail jobs to prevent CPU spikes on Pi Zero/low-power boards
- [ ] Add thumbnail request backoff/batching (client + server) to reduce repeated placeholder fetches and network churn
- [ ] Enable gzip/deflate (or precompressed) static assets with far-future cache headers to cut bandwidth and page load time
- [ ] Short-lived cache for mode tokens and mount state (with invalidation on mode switch) to reduce repeated sysfs/state lookups

## User Experience (Priority: High)
- [X] Tighten small-screen ergonomics on Videos/Chimes/Light Shows (DONE: Responsive mobile cards, collapsible sections, mobile-optimized layout)
- [ ] **Bulk Video Selection**: Add checkboxes and "Delete Selected" button for managing specific videos (alternative to "all or nothing")
- [ ] **Video Search/Filter**: Add search box with live filtering by filename, date, camera, or session ID for large libraries
- [X] Show clearer mode-switch progress (DONE: Loading overlay with spinner, operation-in-progress banner with countdown)
- [X] **Progress Indicators**: Add progress bars using JavaScript + Server-Sent Events or polling for file uploads, bulk deletes, mode switches (DONE: XHR upload progress bars on chimes, light shows, and videos)
- [ ] **Toast Notifications**: Replace flash messages with modern JavaScript toast library for non-blocking success/error notifications
- [ ] **Keyboard Shortcuts**: Add power-user shortcuts (`?`=help, `p`=present mode, `e`=edit mode, `v`=videos, `c`=chimes)
- [ ] Provide filtering/search in Chimes and Light Shows to quickly find items in large libraries
- [ ] Add per-item status badges (read-only, pending conversion, invalid format) and inline remediation tips
- [X] **Chime Preview Before Upload**: Add client-side audio preview in upload form to avoid uploading wrong files (DONE: In-browser audio player for all chimes in library)
- [ ] **Quick Actions Menu**: Add floating action button (FAB) with quick access to upload, mode switch, refresh
- [ ] **Better Error Messages**: Replace generic "Error: ..." with specific, actionable messages with troubleshooting hints (PARTIAL: Some good messages, could be improved)
- [ ] **Confirmation Modals**: Replace ugly JavaScript `confirm()` with custom modal dialogs
- [X] **Empty State Messages**: Add helpful first-run messages like "Your Tesla hasn't recorded any videos yet. Try honking to save a clip!" (DONE: "No videos found", "No chimes found" messages)
- [X] **Loading Skeletons**: Add CSS skeleton loaders instead of blank pages while data loads (DONE: Spinner animations for infinite scroll, upload progress)
- [ ] **Favicon & App Icons**: Add Tesla/USB themed favicon and touch icons for browser tabs and mobile home screen
- [ ] Support HTTP range/chunked video streaming so seeks/previews don’t force full-file downloads

## Configuration & Maintenance (Priority: Medium)
- [X] **Web-Based Configuration Editor**: Add settings page to edit `config.sh` and `config.py` (AP credentials, cleanup policies, port) without SSH (DONE: Settings page has AP config, WiFi config, mode control, cleanup settings)
- [ ] **Logging Dashboard**: Add logs page showing recent entries from `journalctl` for all gadget services (no SSH required)
- [X] **Disk Space Warnings**: Add prominent warning banner when >85% full, critical alert at >95% to prevent recording failures (DONE: Analytics dashboard shows warnings/critical alerts with health indicators)
- [ ] **Health Check Dashboard**: Add status page showing CPU temp, disk I/O, mount health, service status, loop device info (PARTIAL: Analytics shows storage health)
- [ ] **One-Click Updates**: Add "Check for Updates" button that runs `git pull` and restart script (with warnings about personal project nature)
- [ ] Web UI control to regenerate/resize the USB images safely (with pre-flight free-space checks and fsck)
- [ ] Event log UI (connect/disconnect, mode switches, uploads/deletes, errors) with export to CSV/JSON
- [ ] Schedule orphan thumbnail cleanup (nightly or on boot) and track last scan to avoid on-demand full-tree walks during browsing

## New Functionality (Priority: Medium)
- [X] **Offline fallback access point**: start hostapd/dnsmasq with static 192.168.4.1 when STA join fails (with RSSI/dwell hysteresis to avoid flaps) so a phone can reach the web UI in the car (DONE: Full AP implementation with auto/force_on/force_off modes, concurrent with WiFi client)
- [ ] Optional cloud/off-Pi backup target (S3/Backblaze/SMB/rsync) with bandwidth caps and scheduled sync windows for important saved clips
- [ ] Scheduling UI to auto-switch between Present/Edit based on time-of-day or WiFi presence to avoid manual toggles
- [ ] Support for OTA/lightweight upgrades that download and stage new releases, then prompt for a controlled restart
- [ ] **Video Export/Share**: Add "Share" button that creates temporary public link or exports selected clips
- [ ] **Chime Library Import**: Add "Import from ZIP" feature for bulk chime collection imports (PARTIAL: Light shows support ZIP import, chimes can bulk upload)
- [X] **Persistent Theme Preference**: Store theme in cookie or server-side user setting instead of just localStorage (syncs across devices) (DONE: Theme stored in localStorage with dark/light modes)

## Reliability (Priority: Medium)
- [ ] Systemd watchdog/healthcheck for `gadget_web.service` and thumbnail generator to auto-restart on hangs
- [ ] Automated pre/post mode-switch diagnostics: verify nsenter mounts, loop devices, Samba state, and bail out with clear errors
- [ ] Periodic fsck of both images while in Edit mode with snapshot/loop isolation and alert on corruption trends
- [ ] Metrics endpoint (Prometheus/text) exposing mode durations, mount failures, thumbnail latency, and Samba reload counts
- [ ] Integration tests for mode switching and file operations (Videos/Chimes/Light Shows) running in CI to catch regressions
- [ ] **Graceful Degradation**: Show informative messages and allow partial functionality when partitions unmounted instead of complete failure (PARTIAL: Error messages shown, but functionality limited)
- [ ] **Retry Logic**: Add exponential backoff retry for transient failures in mount operations and subprocess calls
- [X] **Lock File Monitoring UI**: Add lock file status to operations banner with "Force Clear" button (with warnings about stuck operations) (DONE: Operation-in-progress banner with countdown and lock age)
- [ ] Offload FFmpeg chime re-encode/normalize to background task with progress polling to keep web workers responsive and avoid watchdog resets

## Reliability (Priority: Medium)
- [ ] Systemd watchdog/healthcheck for `gadget_web.service` and thumbnail generator to auto-restart on hangs
- [ ] Automated pre/post mode-switch diagnostics: verify nsenter mounts, loop devices, Samba state, and bail out with clear errors
- [ ] Periodic fsck of both images while in Edit mode with snapshot/loop isolation and alert on corruption trends
- [ ] Metrics endpoint (Prometheus/text) exposing mode durations, mount failures, thumbnail latency, and Samba reload counts
- [ ] Integration tests for mode switching and file operations (Videos/Chimes/Light Shows) running in CI to catch regressions
- [ ] **Graceful Degradation**: Show informative messages and allow partial functionality when partitions unmounted instead of complete failure (PARTIAL: Error messages shown, but functionality limited)
- [ ] **Retry Logic**: Add exponential backoff retry for transient failures in mount operations and subprocess calls
- [X] **Lock File Monitoring UI**: Add lock file status to operations banner with "Force Clear" button (with warnings about stuck operations) (DONE: Operation-in-progress banner with countdown and lock age)

## Analytics & Insights (Priority: Low)
- [ ] **Storage Trend Graphs**: Track daily storage growth and project "days until full" instead of just point-in-time data
- [X] **Recording Statistics**: Show clips per day, busiest times, camera distribution, sentry vs regular recording patterns (DONE: Analytics dashboard shows per-folder breakdown, oldest/newest dates, size distribution)
- [X] **Chime Usage Tracking**: Track chime activation history and show most-used favorites (DONE: Chime schedules track which chime is active at what times)

## Mobile Experience (Priority: Low)
- [X] **Improved Mobile UI**: Increase touch target sizes and improve spacing for better in-car usage on mobile viewports (DONE: Mobile-optimized cards, large buttons, responsive layout)
- [X] **Mobile Video Playback Optimization**: Add lower-quality streaming option or adaptive bitrate for smoother in-car video review (DONE: Low bandwidth mode with preload="none" streaming)


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
2. **Input Sanitization** - Critical security fix (safe_join) - PARTIAL DONE (os.path.basename used)
3. ~~**Lazy Load Thumbnails**~~ - ✅ DONE (HTML loading="lazy" attribute)
4. **Bulk Video Selection** - Most requested feature (checkboxes + batch delete)
5. ~~**Disk Space Warning**~~ - ✅ DONE (Analytics dashboard with health alerts)
6. **Better Error Messages** - Reduces confusion (improve flash messages) - PARTIAL
7. **Video Search/Filter** - Essential for large libraries (client-side JS filter)
8. ~~**Progress Indicators**~~ - ✅ DONE (XHR upload progress bars everywhere)
9. **Persistent Secret Key** - Fixes session issues (save to .env file)
10. **Rate Limiting** - Prevents abuse (Flask-Limiter)

---

## Summary of Completed Features

### ✅ Fully Implemented
- **Offline Access Point**: Full concurrent AP with auto/force modes, WiFi monitoring, web UI control
- **Analytics Dashboard**: Storage usage, health monitoring, warning/critical alerts, folder breakdown
- **Auto-Cleanup System**: Configurable policies (age/size/count), preview/execute, dry-run mode
- **Video Management**: Multi-camera session view, thumbnails, infinite scroll, delete all
- **Lock Chimes**: Upload WAV/MP3, auto-conversion, audio trimmer with waveform, volume normalization, scheduling system
- **Light Shows**: Upload .fseq/.mp3/.wav, ZIP import support, partition-aware management
- **Progress Indicators**: XHR upload progress bars on all file uploads
- **Mobile Optimizations**: Responsive cards, low bandwidth mode, mobile-friendly layout
- **Theme System**: Dark/light mode toggle with localStorage persistence
- **Upload Validation**: File type and size validation on all uploads
- **Background Processing**: Systemd timers for thumbnails and chime scheduling

### 🟡 Partially Implemented
- **Security**: File validation done, but missing CSRF, rate limiting, authentication, HTTPS
- **Error Messages**: Some good messages, but could be more specific and actionable
- **Input Sanitization**: Using os.path.basename() but not werkzeug.safe_join()
- **Health Dashboard**: Storage health shown, but missing CPU temp, I/O stats, service status

### 🔴 Not Yet Implemented
Critical security items (CSRF, authentication, HTTPS), advanced features (video search/filter, bulk selection, cloud backup), and nice-to-haves (keyboard shortcuts, PWA, marketplace)
