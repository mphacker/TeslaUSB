# Configuration System

How TeslaUSB loads, exposes, and reloads configuration. After this
doc you should understand exactly where every value comes from at
runtime, why we use two thin wrappers (one Bash, one Python), and
how the template-substitution layer turns `config.yaml` values into
deployed paths.

---

## At a glance

- **Single source of truth**: `config.yaml` at the repo root. Holds
  paths, credentials, network settings, retention thresholds,
  indexing parameters — everything.
- **Two thin wrappers**: `scripts/config.sh` for Bash,
  `scripts/web/config.py` for Python. Both load and cache the YAML
  once.
- **Templates**: source files under `scripts/` and `templates/`
  contain `__GADGET_DIR__`-style placeholders. `setup_usb.sh`
  substitutes them and writes the result into systemd / sshd / NM
  dispatcher locations.
- **Reload model**: most changes take effect on `systemctl restart
  gadget_web.service`. AP/STA changes also need
  `systemctl restart wifi-monitor.service`.

---

## File: `config.yaml`

Lives at the **repo root**, not inside `scripts/`. Edited in place
on the device. Annotated with comments explaining each key.

The top-level structure (see the file for everything):

```yaml
installation:        # target user, mount dir
disk_images:         # cam_name, lightshow_name, music_*, boot_fsck_enabled
setup:               # partition sizes, archive_reserve_size (used by setup_usb.sh)
network:             # samba_password, web_port (80 for captive portal)
offline_ap:          # SSID, passphrase, channel, force_mode, etc.
system:              # config.txt path, smb.conf path
web:                 # secret_key, lock-chime constraints, upload limits
mapping:             # indexing on/off, sample_rate, trip_gap_minutes,
                     # event_detection thresholds, index_too_new_seconds
cloud_archive:       # provider, remote_path, bandwidth limit, retention,
                     # delete_unsynced, sync_non_event_videos, dead-letter
cleanup:             # default_retention_days, free_space_target_pct,
                     # max_archive_size_gb, per-folder policies
archive:             # rescan interval, worker tuning, _atomic_copy guards,
                     # boot_scan_defer_seconds, retention_days fallback
live_event_sync:     # enabled, upload_scope, retry backoff, daily cap, webhook
```

A handful of keys were **deprecated** in earlier waves and have now
been removed (issue #184 Wave 1). If your `config.yaml` still mentions
them, the new code simply ignores them — no upgrade hazard, but
operators may want to clean them up:

- `mapping.index_on_startup` and `mapping.index_on_mode_switch` —
  replaced by the persistent indexing queue + worker, which always
  runs.
- `mapping.archive_indexing` — never consumed; ArchivedClips
  indexing is intrinsic to the indexer.
- `mapping.event_detection.fsd_disengage_detect` — FSD-disengage
  detection is now always on.
- `archive.only_driving` — never consumed; replaced by the
  unconditional SEI-peek skip.
- `archive.skip_stationary_recent_clips` — now unconditional; the
  archive worker always SEI-peeks RecentClips and marks
  parked-no-event clips `skipped_stationary` instead of copying.
  Sentry/Saved event clips are never skipped.
- `archive_queue.enabled` and `archive_queue.boot_catchup_enabled` —
  the archive queue subsystem is unconditional; disabling it left
  the gadget in a degraded state with no recourse for catch-up
  after an unclean shutdown.

---

## Wrapper: `scripts/config.sh` (Bash)

Sourced by every Bash script that needs a config value. Single
optimized `yq` call evaluated as Bash assignments — saves about
1.2 s per invocation versus calling `yq` per key.

Typical use inside a Bash script:

```bash
# At the top of the script:
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/config.sh"

# Now config values are exported as Bash variables:
echo "$GADGET_DIR"           # e.g. /mnt/gadget
echo "$TARGET_USER"          # e.g. pi
echo "$IMG_CAM_NAME"         # e.g. usb_cam.img
```

Important: **all values in the eval are double-quoted** so a value
containing special characters can never inject Bash. This is a
deliberate security control — `yq | eval` without quoting was a
known injection path.

When you add a new config key that Bash needs, add it to
`config.sh`'s eval line in the same quoted form.

---

## Wrapper: `scripts/web/config.py` (Python)

Imported by every Python module that needs a config value. Single
PyYAML load at module-import time, cached as module-level constants.

Typical use:

```python
from scripts.web import config

print(config.GADGET_DIR)         # str
print(config.IMG_CAM_PATH)       # computed: GADGET_DIR + IMG_CAM_NAME
print(config.MUSIC_ENABLED)      # bool
print(config.LIVE_EVENT_SYNC_ENABLED)
```

Computed values (paths assembled from multiple keys) are exported as
top-level constants too, so callers don't have to assemble them by
hand. Examples:

- `IMG_CAM_PATH = os.path.join(GADGET_DIR, IMG_CAM_NAME)`
- `IMG_LIGHTSHOW_PATH = …`
- `IMG_MUSIC_PATH = …`
- `ARCHIVE_DIR = os.path.expanduser("~/ArchivedClips")`

These are **the strings you should pass around**, not raw config
keys. `os.path.isfile(IMG_CAM_PATH)` is the canonical "is the
TeslaCam drive present?" check used by the image-gating layer.

When you add a new config key that Python needs, add it to
`config.py` as a module-level constant alongside the others. Stay
typed — booleans as `bool`, paths as fully-resolved `str`, lists
as actual `list`.

---

## Image gating

A pattern the docs cite repeatedly: each blueprint that depends on
a particular `.img` file checks at request-time whether the file
exists, and gates routes / nav links accordingly.

```python
# In a blueprint:
@bp.before_request
def _gate_on_cam_image():
    if not os.path.isfile(config.IMG_CAM_PATH):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(error="cam image missing"), 503
        flash("TeslaCam drive not configured")
        return redirect(url_for("settings_advanced.index"))
```

The matching nav-link guard is in the base template:

```jinja
{% if videos_available %}
<a href="{{ url_for('mapping.index') }}">Map</a>
{% endif %}
```

The `videos_available` / `analytics_available` / `chimes_available`
booleans are populated by `partition_service.get_feature_availability()`
(merged into every template context by `utils.get_base_context()`).
That function does the `os.path.isfile()` checks per request — it
doesn't cache, because the user may add or remove `.img` files at
any time without restarting.

When you add a new feature gated on a new `.img` file:

1. Add the path constant in `config.py`.
2. Add the feature-availability flag in
   `partition_service.get_feature_availability()`.
3. Wrap the nav link in `base.html` (both desktop and mobile menus).
4. Add a `@bp.before_request` guard in the blueprint.

---

## Template substitution

Source files under `scripts/` and `templates/` contain placeholders
that `setup_usb.sh` rewrites during installation. The placeholders:

| Placeholder           | Substituted with                                                |
|-----------------------|-----------------------------------------------------------------|
| `__GADGET_DIR__`      | `installation.mount_dir` (default `/mnt/gadget`)                 |
| `__MNT_DIR__`         | Same — alias                                                     |
| `__TARGET_USER__`     | `installation.target_user` (auto-overridden by `SUDO_USER`)      |
| `__IMG_NAME__`        | `disk_images.cam_name` etc., depending on context                |
| `__SECRET_KEY__`      | `web.secret_key` (auto-generated on first install if default)    |

Examples of substituted files:

- `templates/gadget_web.service` → `/etc/systemd/system/gadget_web.service`
- `templates/99-teslausb-cloud-refresh` →
  `/etc/NetworkManager/dispatcher.d/99-teslausb-cloud-refresh`
- `scripts/present_usb.sh` → executed in place after substitution
- `scripts/edit_usb.sh` → same

After substitution, the deployed file has **fully resolved paths**.
The repo source has **only placeholders**. The rule is:

> **Never hardcode `/home/pi/...` or `/mnt/gadget/...` in source.**
> Always use placeholders so the same source works on any device.

---

## Reload model

### When `systemctl restart gadget_web.service` is enough

- Any change to `mapping.*`, `cloud_archive.*`, `cleanup.*`,
  `live_event_sync.*`, `archive.*`, `web.*`
- Any change to a Python service or blueprint
- Any change to a Jinja template
- Any change to a static asset

### When `wifi-monitor.service` also needs a restart

- Any change to `offline_ap.*`

### When `setup_usb.sh` re-run is required

- Any change to a file under `templates/` (systemd unit, NM
  dispatcher, sshd drop-in)
- Any change to `installation.target_user` (re-substitutes
  everything)
- Any change to a placeholder-using shell script

The setup script is interactive and re-runs many steps; for a
one-line template change, `sed`-substituting the placeholders by
hand and `systemctl daemon-reload` is faster.

### When a reboot is required

- Any change to `disk_images.boot_fsck_enabled`
- Any change to `installation.mount_dir`
- Any change to `disk_images.music_enabled` from `true` to `false`
  (the gadget config has to be regenerated and the kernel module
  reloaded)

---

## Versioning and migrations

`config.yaml` does not carry a version number. Backward-compatible
key additions ship without ceremony — the wrappers default-init
missing keys to sensible values and log a warning at startup when
they do.

For breaking changes (key renames, removals), the procedure is:

1. Keep the old key as a backward-compat fallback in the wrapper for
   one release.
2. Ship a deprecation comment in the YAML.
3. Flag it in the readme's "CHANGES FOR EXISTING USERS" callout.
4. Remove the fallback in a later release.

A precedent for this is the `cleanup.default_retention_days` →
old-`cloud_archive.archived_clips_retention_days` →
old-`archive.retention_days` fallback chain in
`archive_watchdog._resolve_retention_days()`. Follow that pattern
when introducing new fallbacks.

---

## Decision points

| Question                                              | Where the decision lives                              | Outcome                                                       |
|-------------------------------------------------------|-------------------------------------------------------|---------------------------------------------------------------|
| Where do `.img` files live?                            | `installation.mount_dir`                              | All `.img` files placed there                                  |
| Should the third (Music) LUN be presented?            | `disk_images.music_enabled`                           | `True` → 3 LUNs at boot; `False` → 2                           |
| Should boot-time fsck run?                            | `disk_images.boot_fsck_enabled`                       | `True` → `fsck -p` on each `.img` before UDC bind              |
| Which port does Flask bind to?                        | `network.web_port`                                    | Should always be `80` for captive portal                       |
| Should LES be active?                                 | `live_event_sync.enabled`                             | `False` (default) → no thread, no callback, no DB writes       |
| Should non-event clips be cloud-uploaded?             | `cloud_archive.sync_non_event_videos`                 | `False` (default) → only events + geolocated clips             |
| When should retention delete an unsynced clip?        | `cloud_archive.delete_unsynced` (`null`/`false`/`true`) | See [`GLOSSARY.md`](../../GLOSSARY.md) under "delete_unsynced" |

---

## Failure modes & recovery

### `config.yaml` is missing or malformed

Both wrappers fail loudly on startup. `gadget_web.service` won't
start; `journalctl -u gadget_web.service` shows the YAML error. Fix
the file and restart.

### Wrapper imports a key that doesn't exist

The Python wrapper logs a WARNING and uses the documented default.
The Bash wrapper uses a `||` default in the eval expression.

### A placeholder reaches the deployed file

A bug in `setup_usb.sh`'s substitution. Symptom: a literal
`__GADGET_DIR__` string appears somewhere. Re-run setup; if it
recurs, file an issue.

### Deprecated key still present

Logged as INFO at startup. No functional impact. Remove the key
from `config.yaml` at the next maintenance window.

---

## Source files

- `config.yaml` — the source of truth
- `scripts/config.sh` — Bash wrapper (single optimized `yq` eval)
- `scripts/web/config.py` — Python wrapper (PyYAML, cached at
  import-time)
- `scripts/web/services/partition_service.py::get_feature_availability()`
  — image-gating booleans
- `scripts/web/utils.py::get_base_context()` — merges availability
  flags into every template context
- `setup_usb.sh` — template substitution + deployment
- `templates/*` — placeholder-bearing source templates
