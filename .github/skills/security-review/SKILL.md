---
name: security-review
description: >
  Perform an extensive security review of the TeslaUSB application covering subprocess
  injection, path traversal, configuration security, mount/gadget safety, network exposure,
  WiFi AP security, file upload validation, root privilege usage, dependency security, and
  data protection. Use when asked to do a security audit, security review, pen test
  assessment, or check for vulnerabilities.
---

# Security Review

Perform a comprehensive, evidence-based security review of the TeslaUSB application. This
skill systematically examines every attack surface — subprocess injection, path traversal,
configuration security, mount/gadget safety, network exposure, WiFi AP security, file upload
validation, root privilege usage, dependency security, and data protection — and produces an
actionable findings report with severity ratings and remediation guidance.

TeslaUSB is a Raspberry Pi Zero 2 W project that creates a USB mass storage gadget for Tesla
vehicles. It runs a Flask web UI on port 80 (no authentication by design — relies on network
isolation), manages disk images via loop devices and the Linux USB gadget subsystem, and
operates in a vehicle where power can drop at any time.

Read `.github/copilot-instructions.md` first to load all project conventions.

---

## Phase 0 — Prerequisites

### GH CLI Authentication

Verify the GH CLI is authenticated (needed for filing security issues):

```bash
gh auth status
```

---

## Phase 1 — Scope Resolution

Determine the review scope based on the user's request. Three modes:

| Mode | Trigger examples | Behavior |
|------|-----------------|----------|
| **full** | "security review", "security audit", "pen test" | Full application review, all phases |
| **targeted** | "review subprocess security", "check path traversal" | Specific phases only |
| **changed** | "security review of recent changes", "security check PR" | Only files changed in a diff/PR |

### Full mode (default)

Execute **all phases** (2–11) against the entire codebase. This is the most thorough review
and should be used for periodic security audits.

### Targeted mode

Execute only the phases relevant to the user's request. Map keywords to phases:

| Keywords | Phase(s) |
|----------|----------|
| subprocess, command, injection, shell, exec | Phase 2 (Subprocess Security) |
| path, traversal, file, upload, download, sanitize | Phase 3 (Path Traversal) |
| config, yaml, eval, credentials, secrets, password | Phase 4 (Configuration Security) |
| mount, umount, nsenter, gadget, loop, LUN, image | Phase 5 (Mount & Gadget Safety) |
| network, port, endpoint, auth, authentication, samba | Phase 6 (Network Exposure) |
| wifi, AP, access point, captive, DNS, dnsmasq | Phase 7 (WiFi AP Security) |
| upload, chime, music, lightshow, wrap, validate | Phase 8 (File Upload Validation) |
| root, sudo, privilege, systemd, service | Phase 9 (Root Privilege Audit) |
| dependency, pip, apt, package, CVE, vulnerability | Phase 10 (Dependency Security) |
| data, privacy, logging, PII, video, telemetry | Phase 11 (Data Protection) |

### Changed mode

Identify changed files (from git diff, PR, or time range) and execute only the phases that
apply to those files. Use the file-to-phase mapping:

| File pattern | Phase(s) |
|-------------|----------|
| `scripts/web/services/*_service.py` with subprocess calls | 2 |
| `scripts/web/blueprints/*.py` with file operations | 3 |
| `config.yaml`, `scripts/config.sh`, `scripts/web/config.py` | 4 |
| `present_usb.sh`, `edit_usb.sh`, `scripts/web/services/partition_mount_service.py` | 5 |
| `scripts/web/web_control.py`, `scripts/web/blueprints/*.py` (routes) | 6 |
| `scripts/wifi-monitor.sh`, `scripts/web/services/ap_service.py` | 7 |
| `scripts/web/blueprints/lock_chimes.py`, `music.py`, `light_shows.py`, `wraps.py` | 8 |
| `*.sh` files with `sudo` calls, `templates/*.service` | 9 |
| `setup_usb.sh` (package installs), any new imports | 10 |
| `scripts/web/services/*.py` with logging, `scripts/web/blueprints/videos.py` | 11 |

---

## Phase 2 — Subprocess & Command Injection

Review all subprocess and command execution for injection vulnerabilities.

### 2.1 — Subprocess Call Inventory

**Files to examine:**
- `scripts/web/services/partition_mount_service.py` — 50+ subprocess calls (mount, umount, losetup, blkid)
- `scripts/web/services/lock_chime_service.py` — FFmpeg encoding
- `scripts/web/services/fsck_service.py` — Filesystem check operations
- `scripts/web/services/ap_service.py` — AP configuration scripts
- `scripts/web/services/wifi_service.py` — NetworkManager control (nmcli)
- `scripts/web/services/samba_service.py` — Samba service control
- `scripts/web/blueprints/mode_control.py` — Shell script invocation
- All `.sh` files under `scripts/`

Search patterns:
```
subprocess\.run|subprocess\.Popen|subprocess\.call|os\.system|os\.popen
```

### 2.2 — Python Subprocess Checks

| Check | What to verify | Severity |
|-------|---------------|----------|
| List-based arguments | `subprocess.run()` uses list args, not a command string | 🔴 Critical |
| No `shell=True` with user input | If `shell=True` is used, no user-controlled data in command | 🔴 Critical |
| No f-string commands | No `f'command {variable}'` passed to `sh -c` | 🔴 Critical |
| Timeout specified | All subprocess calls have `timeout=` to prevent hangs | 🟡 Warning |
| Return code checking | `check=True` or manual returncode validation | 🟡 Warning |
| Stderr capture | `capture_output=True` or `stderr=subprocess.PIPE` for error logging | 🔵 Info |

**Known risk pattern** — watch for:
```python
# DANGEROUS: f-string in shell command
subprocess.run(['sudo', 'sh', '-c', f'echo "{user_input}" > {path}'], ...)

# SAFE: Use list args and write file directly
with open(path, 'w') as f:
    f.write(user_input)
```

### 2.3 — Bash Script Checks

| Check | What to verify | Severity |
|-------|---------------|----------|
| Variable quoting | All `$VAR` expansions are double-quoted: `"$VAR"` | 🔴 Critical |
| eval safety | `eval` statements have properly quoted values | 🔴 Critical |
| No backtick substitution with user data | Command substitution uses `$()` not backticks | 🟡 Warning |
| Input validation | Script arguments validated before use in commands | 🟡 Warning |

### 2.4 — eval in config.sh

**Specific file to examine:** `scripts/config.sh`

The config loader uses `eval "$(yq ...)"` to load YAML values into shell variables.

| Check | What to verify | Severity |
|-------|---------------|----------|
| All values double-quoted | Every value in the yq template is wrapped: `"\"" + value + "\""` | 🔴 Critical |
| No unquoted expansions | No `eval` with values that could contain `$(...)` or backticks | 🔴 Critical |
| safe_load equivalent | yq output cannot execute arbitrary commands | 🟡 Warning |

---

## Phase 3 — Path Traversal & File Handling

Review all file operations for path traversal, symlink attacks, and unsafe file handling.

### 3.1 — File Operation Inventory

**Files to examine:**
- `scripts/web/blueprints/videos.py` — Video streaming, download, SEI parsing
- `scripts/web/blueprints/lock_chimes.py` — Chime upload, rename, delete
- `scripts/web/blueprints/light_shows.py` — Light show upload, delete
- `scripts/web/blueprints/music.py` — Music file upload (chunked), delete, rename
- `scripts/web/blueprints/wraps.py` — Wrap image upload, delete
- `scripts/web/services/video_service.py` — Video file discovery
- `scripts/web/services/music_service.py` — Music file operations
- `scripts/web/services/lock_chime_service.py` — Chime file management

Search patterns:
```
os\.path\.join|open\(|send_file|send_from_directory|os\.listdir|os\.rename|shutil
```

### 3.2 — Path Traversal Checks

| Check | What to verify | Severity |
|-------|---------------|----------|
| `os.path.basename()` on user input | Filenames from requests stripped of directory components | 🔴 Critical |
| `os.path.commonpath()` containment | Resolved paths verified to stay within allowed directory | 🔴 Critical |
| No `..` in paths | Path components checked for parent directory traversal | 🔴 Critical |
| Symlink resolution | `os.path.realpath()` used before containment check (prevents symlink escape) | 🟡 Warning |
| Extension validation | Only expected file extensions accepted for uploads | 🟡 Warning |

**TeslaUSB-specific pattern to verify:**
```python
# CORRECT: music_service.py pattern
def _resolve_subpath(mount_path, rel_path):
    target = os.path.join(mount_path, rel)
    common = os.path.commonpath([mount_path, target])
    if common != os.path.abspath(mount_path):
        raise MusicServiceError("Invalid path")
    return target
```

Verify **all** blueprints that accept file paths from requests follow this pattern.

### 3.3 — File Upload Safety

| Check | What to verify | Severity |
|-------|---------------|----------|
| Size limits enforced | `MAX_CONTENT_LENGTH` set in Flask app config | 🟡 Warning |
| Content validation | File content validated (not just extension) — e.g., WAV header check | 🟡 Warning |
| Temp file cleanup | Temporary files cleaned up on failure/exception | 🟡 Warning |
| Atomic write | Upload uses temp → fsync → rename pattern | 🟡 Warning |

### 3.4 — Video Streaming Endpoint

**Specific attention:** `scripts/web/blueprints/videos.py` — `/stream/<path:filepath>`

This endpoint serves arbitrary video files via path parameter. Verify:
- Path components are sanitized with `os.path.basename()`
- Resolved path stays within TeslaCam directory
- No directory listing exposure
- HTTP Range requests don't expose file size information beyond the served file

---

## Phase 4 — Configuration Security

Review configuration file handling for credential exposure and injection risks.

### 4.1 — Credential Storage

**Files to examine:**
- `config.yaml` — Contains Samba password, AP passphrase, Flask secret key
- `scripts/web/config.py` — Loads and exposes config values to Python

| Check | What to verify | Severity |
|-------|---------------|----------|
| No credentials in source code | Passwords, keys only in `config.yaml` (user-edited) | 🔴 Critical |
| `config.yaml` in `.gitignore` | Config file with credentials not committed to git | 🟡 Warning |
| Secret key generation | Flask `SECRET_KEY` auto-generated if default value detected | 🟡 Warning |
| `yaml.safe_load()` | Python uses `safe_load()`, not `yaml.load()` (prevents code execution) | 🔴 Critical |

### 4.2 — Default Credentials

| Check | What to verify | Severity |
|-------|---------------|----------|
| Default Samba password | Check if default `tesla` password is flagged to user during setup | 🟡 Warning |
| Default AP passphrase | Check if default `teslausb1234` passphrase is flagged | 🟡 Warning |
| Default Flask secret | Check if default secret key is detected and regenerated | 🟡 Warning |

### 4.3 — Config Validation

| Check | What to verify | Severity |
|-------|---------------|----------|
| Required fields checked | Missing required config values cause clear error, not crash | 🟡 Warning |
| Type validation | Numeric fields validated as numbers, paths as valid paths | 🔵 Info |
| Size format validation | Disk image sizes validated (e.g., `512M`, `5G` format) | 🟡 Warning |

---

## Phase 5 — Mount & USB Gadget Safety

Review mount operations, loop device management, and USB gadget configuration for safety
issues that could cause data corruption, mount leaks, or kernel locks.

### 5.1 — Mount Namespace Compliance

**Files to examine:**
- `present_usb.sh` — Gadget presentation
- `edit_usb.sh` — Edit mode transition
- `scripts/web/services/partition_mount_service.py` — Mount management service

| Check | What to verify | Severity |
|-------|---------------|----------|
| `nsenter` wrapping | ALL mount/umount/mountpoint commands use `sudo nsenter --mount=/proc/1/ns/mnt` | 🔴 Critical |
| No bare mount calls | No `subprocess.run(['sudo', 'mount', ...])` without nsenter | 🔴 Critical |
| Namespace verification | Scripts verify they're in the correct namespace before proceeding | 🟡 Warning |

### 5.2 — Loop Device Safety

| Check | What to verify | Severity |
|-------|---------------|----------|
| RO loop not mounted RW | Read-only loop devices (`-r` flag) never mounted with `rw` options | 🔴 Critical |
| LUN cleared before detach | Gadget LUN backing file cleared before detaching loop devices | 🔴 Critical |
| Stale loop cleanup | Old loop devices cleaned up before creating new ones | 🟡 Warning |
| Loop device leak | All code paths detach loop devices (including error paths) | 🟡 Warning |

### 5.3 — Quick-Edit Safety

| Check | What to verify | Severity |
|-------|---------------|----------|
| Lock file usage | Operations use `.quick_edit_part2.lock` with 120s stale timeout | 🔴 Critical |
| Cleanup on all paths | Exception handlers restore RO mount and LUN backing | 🔴 Critical |
| Operation brevity | Quick-edit operations are short (don't hold lock longer than necessary) | 🟡 Warning |
| Sequence correctness | Clear LUN → unmount RO → detach → create RW → mount → work → sync → unmount → detach → create RO → remount → restore LUN | 🔴 Critical |

### 5.4 — Sync & Power-Loss Safety

| Check | What to verify | Severity |
|-------|---------------|----------|
| Sync before gadget ops | `sync` called before unbinding/rebinding USB gadget | 🔴 Critical |
| Sync after writes | `sync` called after filesystem writes before re-presenting | 🔴 Critical |
| Atomic file replacement | File writes use temp + fsync + rename pattern | 🟡 Warning |
| fsck on boot | Boot fsck enabled to recover from power-loss corruption | 🟡 Warning |

---

## Phase 6 — Network Exposure & Authentication

Review the network attack surface. TeslaUSB runs without authentication by design (relies
on WiFi network isolation), but this must be validated.

### 6.1 — Web Server Exposure

**Files to examine:**
- `scripts/web/web_control.py` — Server binding configuration
- All blueprint files — Route definitions

| Check | What to verify | Severity |
|-------|---------------|----------|
| Bind address | Server binds to `0.0.0.0` (expected for AP access) | 🔵 Info |
| Port 80 | Runs on port 80 for captive portal (expected, documented) | 🔵 Info |
| No sensitive data in responses | API endpoints don't expose system credentials | 🔴 Critical |
| Error page info leakage | Error responses don't expose stack traces, file paths, or config | 🟡 Warning |
| Debug mode disabled | Flask `debug=False` in production (Waitress mode) | 🔴 Critical |

### 6.2 — Route Security Audit

For each exposed route, verify:

| Check | What to verify | Severity |
|-------|---------------|----------|
| State-changing methods | POST/DELETE routes validate expected inputs | 🟡 Warning |
| File serving routes | Only serve files from expected directories | 🔴 Critical |
| Mode-changing routes | Mode switch routes validate current state before proceeding | 🟡 Warning |
| API data exposure | Status/analytics APIs don't expose credentials or internal paths | 🟡 Warning |

### 6.3 — Samba Exposure

**Files to examine:**
- `scripts/web/services/samba_service.py`
- Samba configuration templates

| Check | What to verify | Severity |
|-------|---------------|----------|
| Share scope | Samba shares only expose expected directories (TeslaCam, LightShow) | 🔴 Critical |
| Authentication | Samba requires password (from config.yaml) | 🟡 Warning |
| Guest access | No anonymous/guest access to Samba shares | 🟡 Warning |
| Edit mode only | Samba only active in edit mode (off in present mode) | 🟡 Warning |

### 6.4 — No-Auth Design Validation

Since TeslaUSB intentionally has no web authentication:

| Check | What to verify | Severity |
|-------|---------------|----------|
| Network isolation documented | README/docs clearly state security relies on WiFi isolation | 🟡 Warning |
| No credential endpoints | No login forms or session management that creates false security | 🔵 Info |
| Destructive operations guarded | Destructive ops (delete files, format, mode switch) have confirmation | 🟡 Warning |

---

## Phase 7 — WiFi AP & Captive Portal Security

Review the offline access point and captive portal for security issues.

### 7.1 — Access Point Configuration

**Files to examine:**
- `scripts/wifi-monitor.sh`
- `scripts/web/services/ap_service.py`
- `config.yaml` (offline_ap section)

| Check | What to verify | Severity |
|-------|---------------|----------|
| WPA2 enforcement | AP uses WPA2 (not WPA or open) | 🔴 Critical |
| Passphrase strength | Default passphrase is at least 8 chars; user warned to change it | 🟡 Warning |
| SSID broadcast | Consider whether SSID hiding should be an option | 🔵 Info |
| Channel selection | Valid WiFi channel numbers used | 🔵 Info |

### 7.2 — DNS Spoofing Scope

**Files to examine:**
- dnsmasq configuration templates
- `scripts/web/blueprints/captive_portal.py`

| Check | What to verify | Severity |
|-------|---------------|----------|
| DNS scope limited | DNS spoofing only active when AP is running | 🟡 Warning |
| No external DNS leak | Spoofed DNS doesn't affect client's DNS for non-captive traffic | 🟡 Warning |
| Portal detection URLs | Only standard OS connectivity-check URLs are intercepted | 🟡 Warning |

### 7.3 — Captive Portal Security

| Check | What to verify | Severity |
|-------|---------------|----------|
| No sensitive data in splash | Captive portal page doesn't expose device config or credentials | 🟡 Warning |
| Portal redirect safety | Redirect URLs are validated (no open redirect vulnerability) | 🟡 Warning |
| HTTPS not broken | Portal doesn't intercept HTTPS connectivity checks (causes cert errors) | 🔵 Info |

### 7.4 — Force Mode Security

| Check | What to verify | Severity |
|-------|---------------|----------|
| Runtime vs persistent | Runtime force mode changes don't persist across reboot (expected) | 🔵 Info |
| Config persistence | Permanent changes go through config.yaml | 🟡 Warning |
| No unauthorized AP start | Force mode can only be changed via web UI (which requires network access) | 🔵 Info |

---

## Phase 8 — File Upload Validation

Review all file upload endpoints for security and safety.

### 8.1 — Upload Endpoint Inventory

**Endpoints to examine:**
- Lock chimes: `/lock_chimes/upload` — WAV file upload
- Light shows: `/light_shows/upload` — FSEQ/MP3/WAV upload
- Music: `/music/upload` — Chunked file upload (any audio format)
- Wraps: `/wraps/upload` — PNG image upload

### 8.2 — Per-Endpoint Checks

| Check | What to verify | Severity |
|-------|---------------|----------|
| File size limits | `MAX_CONTENT_LENGTH` or per-endpoint size check | 🟡 Warning |
| Extension whitelist | Only expected extensions accepted (not blacklist) | 🟡 Warning |
| Content validation | File header/magic bytes verified (not just extension) | 🟡 Warning |
| Filename sanitization | `os.path.basename()` + character stripping applied | 🔴 Critical |
| Directory containment | Saved file path verified within expected directory | 🔴 Critical |
| Temp file cleanup | Temporary files removed on upload failure | 🟡 Warning |
| Disk space check | Available space verified before accepting large uploads | 🔵 Info |

### 8.3 — Lock Chime Specific

| Check | What to verify | Severity |
|-------|---------------|----------|
| WAV format validation | 16-bit PCM, 44.1/48 kHz, mono/stereo enforced | 🟡 Warning |
| Size limit | < 1 MiB enforced | 🟡 Warning |
| Duration limit | ≤ 10 seconds enforced | 🟡 Warning |
| FFmpeg injection | No user input passed to FFmpeg command line unsanitized | 🔴 Critical |
| Atomic replacement | `LockChime.wav` replaced via temp + fsync + MD5 verification | 🟡 Warning |

### 8.4 — Music Upload (Chunked)

| Check | What to verify | Severity |
|-------|---------------|----------|
| Chunk size limit | Individual chunks limited (16MB configured) | 🟡 Warning |
| Total size limit | Overall file size enforced across chunks | 🟡 Warning |
| Chunk reassembly safety | Chunks validated and assembled securely | 🟡 Warning |
| Partial upload cleanup | Incomplete uploads cleaned up on timeout/abort | 🟡 Warning |

---

## Phase 9 — Root Privilege & Systemd Audit

Review the use of root privileges throughout the application.

### 9.1 — sudo Call Inventory

**Search for all sudo usage:**
```
subprocess.*sudo|sudo\s|nsenter.*sudo
```

| Check | What to verify | Severity |
|-------|---------------|----------|
| Minimum privilege | Each sudo call uses the minimum required command | 🟡 Warning |
| Sudoers configuration | Passwordless sudo limited to specific commands (not ALL) | 🟡 Warning |
| No arbitrary sudo | User input never reaches sudo command arguments | 🔴 Critical |
| Timeout on sudo calls | All sudo subprocess calls have timeout parameter | 🟡 Warning |

### 9.2 — Systemd Service Security

**Files to examine:**
- `templates/gadget_web.service` — Web service unit
- `templates/wifi-monitor.service` — WiFi monitor unit
- Other `.service` and `.timer` files in `templates/`

| Check | What to verify | Severity |
|-------|---------------|----------|
| Service user | Consider whether services can run as non-root (port 80 requires root or CAP_NET_BIND_SERVICE) | 🟡 Warning |
| Restart policy | Services auto-restart on failure (Restart=on-failure) | 🔵 Info |
| Working directory | WorkingDirectory set correctly (not world-writable) | 🟡 Warning |
| Environment isolation | No sensitive environment variables leaked to child processes | 🟡 Warning |

### 9.3 — Web Server Privilege

| Check | What to verify | Severity |
|-------|---------------|----------|
| Port 80 binding | Server runs as root to bind port 80 (documented, required for captive portal) | 🔵 Info |
| Privilege drop | Consider whether server can drop privileges after binding port 80 | 🟡 Warning |
| File permissions | Created files have appropriate permissions (not world-writable) | 🟡 Warning |

---

## Phase 10 — Dependency Security

Review Python packages and system dependencies for known vulnerabilities.

### 10.1 — Python Package Audit

**Files to examine:**
- `setup_usb.sh` — Contains inline pip install commands
- Python imports across all `.py` files

| Check | What to verify | Severity |
|-------|---------------|----------|
| Known CVEs | Check installed packages against known vulnerabilities | 🔴 Critical (if exploitable) |
| Pinned versions | Package versions pinned for reproducibility | 🟡 Warning |
| requirements.txt exists | Dependencies documented in a requirements file | 🟡 Warning |
| No unnecessary packages | Only needed packages installed | 🔵 Info |

Run if pip is available:

```bash
pip3 list --outdated 2>/dev/null
pip3 audit 2>/dev/null  # if pip-audit is installed
```

### 10.2 — System Package Audit

| Check | What to verify | Severity |
|-------|---------------|----------|
| Apt packages current | System packages updated during setup | 🟡 Warning |
| yq source trusted | yq installed from trusted source (apt or official release) | 🟡 Warning |
| FFmpeg version | FFmpeg is recent enough to avoid known CVEs | 🟡 Warning |

### 10.3 — JavaScript Dependencies

**Files to examine:**
- `scripts/web/static/js/*.js` — Vendored JavaScript libraries

| Check | What to verify | Severity |
|-------|---------------|----------|
| Library versions | protobuf.js and other JS libs are reasonably current | 🟡 Warning |
| No CDN for critical libs | Security-critical JS loaded locally, not from third-party CDN | 🔵 Info |
| Subresource integrity | If CDN is used, SRI hashes present | 🟡 Warning |

---

## Phase 11 — Data Protection & Privacy

Review how sensitive data (video, telemetry, credentials) is handled.

### 11.1 — Video & Telemetry Data

**Files to examine:**
- `scripts/web/blueprints/videos.py` — Video streaming and SEI telemetry
- `scripts/web/services/video_service.py` — Video file discovery

| Check | What to verify | Severity |
|-------|---------------|----------|
| No video data exfiltration | Videos only served to local network, not uploaded to cloud | 🔴 Critical |
| Telemetry privacy | GPS/speed/telemetry data from SEI not logged or exposed via API | 🟡 Warning |
| Directory listing | Video endpoints don't expose filesystem structure beyond TeslaCam | 🟡 Warning |

### 11.2 — Logging Hygiene

**Files to examine:**
- All `.py` files with `logger.` or `logging.` calls

| Check | What to verify | Severity |
|-------|---------------|----------|
| No credentials in logs | Passwords, secret keys, AP passphrases never logged | 🔴 Critical |
| No file content in logs | Video data, chime audio not logged | 🟡 Warning |
| Path information | Full filesystem paths in logs are acceptable (local device only) | 🔵 Info |
| Log level appropriate | Production logs at INFO or above (not DEBUG) | 🔵 Info |

### 11.3 — Config Credential Protection

| Check | What to verify | Severity |
|-------|---------------|----------|
| config.yaml permissions | File permissions restrict read access (not world-readable) | 🟡 Warning |
| No credentials in API responses | Status/analytics APIs don't return Samba password or AP passphrase | 🔴 Critical |
| No credentials in HTML | Templates don't render credentials in page source | 🔴 Critical |

### 11.4 — USB Data Safety

| Check | What to verify | Severity |
|-------|---------------|----------|
| Samba isolation | Samba shares don't expose system directories (only disk image contents) | 🔴 Critical |
| Disk image boundaries | Web UI file operations cannot access SD card system files | 🔴 Critical |
| Boot fsck safety | fsck operations don't expose file content, only status | 🟡 Warning |

---

## Phase 12 — Report & Remediation

### 12.1 — Compile Findings

Organize all findings into a structured report grouped by severity:

```markdown
# Security Review Report

**Date:** {date}
**Scope:** {full/targeted/changed}
**Reviewer:** Copilot Security Review Skill

## Summary

| Severity | Count |
|----------|-------|
| 🔴 Critical | N |
| 🟡 Warning | N |
| 🔵 Info | N |

## 🔴 Critical Findings

### S-001: {Title}
- **Phase:** {phase number and name}
- **Location:** `path/to/file.py` L{line}
- **Description:** {what the vulnerability is}
- **Impact:** {what an attacker could achieve}
- **Evidence:** {code snippet or search result proving the finding}
- **Remediation:** {specific steps to fix}

## 🟡 Warning Findings
{same format}

## 🔵 Informational Findings
{same format}

## Positive Findings

List security controls that are correctly implemented — this validates existing defenses
and prevents future regressions:

- ✅ {control description} — `path/to/file.py`
```

### 12.2 — File GitHub Issues

For each 🔴 Critical and 🟡 Warning finding, create a GitHub issue:

**Issue title format:** `security: {brief description}`

**Issue body format:**
```markdown
## Security Finding: {ID}

**Severity:** 🔴 Critical / 🟡 Warning

**Description:**
{Detailed description of the vulnerability}

**Location:**
`path/to/file.py` L{line}

**Evidence:**
```python
{code snippet}
```

**Impact:**
{What an attacker could achieve by exploiting this}

**Remediation:**
{Specific steps to fix the vulnerability}

**Discovered during:** Security review on {date}

<!-- skill:security-review:finding:{finding-id} -->
```

**Labels:** Apply `security` label to all security issues. Add `priority: critical` for
🔴 Critical findings.

Before creating issues, check for duplicates:

```bash
gh issue list --repo mphacker/TeslaUSB --state open --label security --json number,title
```

Search by title keywords to avoid duplicates.

### 12.3 — Post Summary Comment (if triggered from an issue/PR)

If the security review was triggered in the context of a specific issue or PR, post a
summary comment with the overall results.

Use marker: `<!-- skill:security-review:summary:{issue-or-pr-number} -->`

### 12.4 — Present Report to User

Display the full report in the terminal. Highlight:

1. **Immediate action required:** 🔴 Critical findings that need urgent remediation
2. **Recommended improvements:** 🟡 Warning findings for the next development cycle
3. **Hardening suggestions:** 🔵 Info findings for defense-in-depth
4. **What's working well:** Positive findings that validate existing security controls

---

## Guardrails

### What this skill does

- Systematically reviews the TeslaUSB application security across 10 domains
- Provides evidence-based findings with file paths, line numbers, and code snippets
- Rates findings using a consistent severity scale (Critical, Warning, Info)
- Files GitHub issues for actionable findings with remediation guidance
- Documents positive security controls to prevent regressions
- Adapts scope based on user request (full, targeted, or changed)

### What this skill does NOT do

- **Does not exploit vulnerabilities** — this is a code review, not a penetration test
- **Does not make code changes** — it identifies and reports; remediation is a separate task
- **Does not scan external infrastructure** — focuses on application code and configuration
- **Does not replace the `review-pr` skill** — review-pr covers code quality and conventions;
  this skill goes deep on security specifically
- **Does not test on live hardware** — reviews code statically; runtime testing on the Pi
  is a separate activity

### Relationship to other skills

| Skill | Relationship |
|-------|-------------|
| `review-pr` | Can invoke security-review in changed mode for PR security assessment |
| `resolve-issue` | Security findings filed as issues can be resolved by resolve-issue skill |

### Quality gates

| Phase | Gate |
|-------|------|
| Phase 1 | Scope clearly defined; applicable phases identified |
| Phases 2–11 | Each phase produces concrete findings with evidence or explicit "no findings" |
| Phase 12 | All Critical/Warning findings have GitHub issues filed; report presented to user |
