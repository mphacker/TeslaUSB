# Repository Layout

Where everything lives in the TeslaUSB source tree, plus the naming
and structural conventions every contributor should know. Use this
as a map when you're hunting for a specific piece of functionality.

---

## Top-level layout

```
TeslaUSB/
├── readme.md                      ← user-facing project overview / install
├── config.yaml                    ← the single source of truth for ALL config
├── setup_usb.sh                   ← one-shot deploy / re-deploy script
├── upgrade.sh                     ← upgrade helper (reapplies setup, restarts)
├── cleanup.sh                     ← optional standalone cleanup invocation
├── pytest.ini                     ← pytest configuration
│
├── docs/                          ← these documents
│   ├── README.md                  ← docs hub
│   ├── ARCHITECTURE.md
│   ├── VIDEO_LIFECYCLE.md         ← the flagship narrative
│   ├── GLOSSARY.md
│   ├── UI_UX_DESIGN_SYSTEM.md
│   ├── operator/                  ← runbooks for deploying / managing devices
│   ├── contributor/               ← internals + contracts (this folder)
│   │   ├── core/                  ← cross-cutting infrastructure
│   │   ├── subsystems/            ← one doc per worker / service
│   │   ├── flows/                 ← end-to-end sequence-diagram traces
│   │   └── reference/             ← lookup tables and API surface
│   └── screenshots/               ← marketing / readme screenshots
│
├── scripts/                       ← runtime code (everything Pi-side)
│   ├── present_usb.sh             ← bring USB gadget UP (present mode)
│   ├── edit_usb.sh                ← bring USB gadget DOWN (edit mode)
│   ├── boot_present_with_cleanup.sh
│   ├── boot_deferred_tasks.sh
│   ├── ap_control.sh              ← offline AP enable/disable wrapper
│   ├── wifi-monitor.sh            ← STA loss → AP fallback service
│   ├── fsck_with_swap.sh          ← boot-time fsck (uses swap on low RAM)
│   ├── optimize_network.sh
│   ├── check_chime_schedule.py    ← periodic chime scheduler tick
│   ├── select_random_chime.py     ← boot-time random chime selection
│   ├── run_boot_cleanup.py        ← deferred post-boot cleanup
│   ├── config.sh                  ← Bash wrapper around config.yaml (uses yq)
│   └── web/                       ← the Flask application
│       ├── web_control.py         ← the Flask `app` factory + entry point
│       ├── config.py              ← Python wrapper around config.yaml
│       ├── utils.py               ← shared template context, helpers
│       ├── blueprints/            ← Flask blueprint modules
│       ├── services/              ← business logic (workers, queues, helpers)
│       ├── helpers/               ← scripts run by external triggers
│       ├── templates/             ← Jinja2 HTML templates
│       └── static/                ← CSS, JS, fonts, icon sprite, images
│
├── templates/                     ← source templates with placeholders
│   ├── 99-teslausb-cloud-refresh  ← NM dispatcher
│   ├── *.service                  ← systemd unit templates
│   ├── *.timer                    ← systemd timer templates
│   ├── sshd-protect.conf          ← sshd hardening drop-in
│   └── teslausb-safe-mode.service ← safe-mode boot detector
│
├── tests/                         ← pytest suite (~1600 tests)
│   ├── conftest.py                ← shared fixtures
│   └── test_*.py                  ← one file per service / blueprint
│
└── .github/
    ├── copilot-instructions.md    ← dense rule-set every PR reviewed against
    ├── workflows/                 ← (none today — TeslaUSB has no CI)
    └── skills/                    ← agent skills (resolve-issue, review-pr, …)
```

---

## `scripts/` (runtime code)

Code that runs on the Pi. Split into shell scripts at the top level and
the Flask application under `web/`.

### Shell entry points

| Script                              | Purpose                                               |
|-------------------------------------|-------------------------------------------------------|
| `present_usb.sh`                    | Mount partitions RO, bind UDC, present gadget         |
| `edit_usb.sh`                       | Unbind UDC, unmount partitions, mount RW, start Samba |
| `boot_present_with_cleanup.sh`      | Wrapper that runs `present_usb.sh` then deferred work |
| `boot_deferred_tasks.sh`            | Post-boot cleanup + random chime selection            |
| `ap_control.sh`                     | Wrapper around hostapd / dnsmasq for AP control       |
| `wifi-monitor.sh`                   | STA-loss detector with exponential AP-retry backoff   |
| `fsck_with_swap.sh`                 | Boot-time `fsck -p` against `usb_cam.img` and `usb_lightshow.img` |
| `config.sh`                         | Bash wrapper around `config.yaml` (uses `yq`)          |

### Python utilities at `scripts/`

| Script                              | Purpose                                               |
|-------------------------------------|-------------------------------------------------------|
| `check_chime_schedule.py`           | Tick called by `chime_scheduler.timer`                |
| `select_random_chime.py`            | Boot-time random chime picker                         |
| `run_boot_cleanup.py`               | Deferred cleanup runner                               |

### `scripts/web/` — the Flask application

```
scripts/web/
├── web_control.py     ← Flask app factory + main entrypoint
├── config.py          ← Python wrapper around config.yaml (uses PyYAML)
├── utils.py           ← shared utilities (context processors, escapers)
├── blueprints/        ← one blueprint per page or API surface
├── services/          ← business logic, workers, queues, parsers
├── helpers/           ← scripts run by external triggers (NM dispatcher)
├── templates/         ← Jinja2 HTML templates
└── static/            ← CSS / JS / fonts / icon sprite / images
```

#### `blueprints/` (HTTP routes)

| File                          | Owns                                                       |
|-------------------------------|------------------------------------------------------------|
| `mapping.py`                  | `/` map dashboard + `/api/index/*` indexer endpoints        |
| `videos.py`                   | Video file serving + delete-event endpoint                  |
| `media.py`                    | Cross-cutting media listing / range-request serving         |
| `analytics.py`                | `/analytics` page                                           |
| `lock_chimes.py`              | Chime upload / preview / set-active / scheduler             |
| `light_shows.py`              | Light show upload / delete                                  |
| `wraps.py`                    | Custom wrap upload / delete                                 |
| `license_plates.py`           | License plate upload / auto-crop                            |
| `boombox.py`                  | Boombox sound upload (Music LUN)                            |
| `music.py`                    | Music drive listing                                         |
| `cleanup.py`                  | Cleanup policies UI and execution                           |
| `cloud_archive.py`            | Cloud sync UI + `/api/cloud/*`                              |
| `live_events.py`              | LES JSON API at `/api/live_events/*`                        |
| `archive_queue.py`            | `/api/archive/*` (status, queue, watchdog)                  |
| `jobs.py`                     | `/jobs` failed-jobs page across all subsystems              |
| `system_health.py`            | `/api/system/health`                                        |
| `mode_control.py`             | Mode-switch endpoints                                       |
| `settings_advanced.py`        | Advanced settings UI                                        |
| `storage_retention.py`        | Storage / retention settings                                |
| `fsck.py`                     | Manual fsck endpoint                                        |
| `captive_portal.py`           | Captive-portal interception URLs                            |
| `api.py`                      | Misc API endpoints not big enough for their own blueprint   |

#### `services/` (business logic)

Grouped by subsystem:

**Configuration / shared infra**

- `config.py` *(see also `scripts/web/config.py`)*
- `task_coordinator.py` — fairness lock for heavy workers
- `crypto_utils.py` — encryption for cloud-provider creds
- `file_safety.py` — atomic-write helpers, IMG-file guard
- `partition_service.py`, `partition_mount_service.py`,
  `mode_service.py`, `samba_service.py` — mount and mode plumbing

**Video pipeline**

- `file_watcher_service.py` — inotify + polling fallback
- `archive_producer.py`, `archive_queue.py`, `archive_worker.py`,
  `archive_watchdog.py`, `video_archive_service.py`
- `indexing_queue_service.py`, `indexing_worker.py`
- `mapping_service.py`, `mapping_queries.py`, `mapping_migrations.py`
- `sei_parser.py` — H.264 SEI extraction (uses `mmap`)
- `dashcam_pb2.py` — protobuf bindings for Tesla's SEI payload
- `video_service.py` — file-listing for the UI
- `analytics_service.py` — aggregate stats from the trip DB

**Cloud**

- `cloud_archive_service.py` — bulk catch-up uploader
- `cloud_oauth_service.py` — OAuth flow for Drive / OneDrive / etc.
- `cloud_rclone_service.py` — rclone subprocess management
- `live_event_sync_service.py` — opt-in real-time uploader
- `bandwidth_test_service.py` — speed test for cloud throughput planning

**Asset management**

- `lock_chime_service.py`, `chime_group_service.py`,
  `chime_scheduler_service.py`
- `light_show_service.py`, `wrap_service.py`,
  `license_plate_service.py`, `boombox_service.py`,
  `music_service.py`

**Networking**

- `wifi_service.py`, `ap_service.py`

**Maintenance**

- `cleanup_service.py`, `fsck_service.py`,
  `clock_skew_repair.py` *(one-shot CLI)*

#### `helpers/` (scripts run by external triggers)

- `refresh_cloud_token.py` — invoked by the NetworkManager
  dispatcher on every WiFi `up` event. Refreshes RO mount, wakes
  LES, waits (bounded) for archive + indexer + LES drain, then
  triggers cloud sync.

#### `templates/` and `static/`

Jinja2 templates and static assets (CSS, JS, fonts, Lucide icon
sprite, images). The UI follows the rules in
[`UI_UX_DESIGN_SYSTEM.md`](../UI_UX_DESIGN_SYSTEM.md).

---

## `templates/` (source templates with placeholders)

These files contain `__GADGET_DIR__`, `__MNT_DIR__`, `__TARGET_USER__`,
`__IMG_NAME__`, `__SECRET_KEY__` placeholders that `setup_usb.sh`
substitutes during installation. **Never hardcode the deployed paths
in source — always use placeholders.**

| File                              | Where it gets installed                                 |
|-----------------------------------|---------------------------------------------------------|
| `gadget_web.service`              | `/etc/systemd/system/`                                  |
| `present_usb_on_boot.service`     | `/etc/systemd/system/`                                  |
| `chime_scheduler.service`         | `/etc/systemd/system/`                                  |
| `chime_scheduler.timer`           | `/etc/systemd/system/`                                  |
| `wifi-monitor.service`            | `/etc/systemd/system/`                                  |
| `network-optimizations.service`   | `/etc/systemd/system/`                                  |
| `teslausb-deferred-tasks.service` | `/etc/systemd/system/`                                  |
| `teslausb-safe-mode.service`      | `/etc/systemd/system/`                                  |
| `99-teslausb-cloud-refresh`       | `/etc/NetworkManager/dispatcher.d/`                     |
| `sshd-protect.conf`               | `/etc/systemd/system/ssh.service.d/`                    |

After editing **any** template or any script under `scripts/` or
`templates/`, run `sudo ./setup_usb.sh` to substitute and deploy,
then restart any affected services.

---

## `tests/`

Approximately 1600 pytest tests covering services and blueprints.
Run the full suite with:

```bash
python -m pytest --tb=short -q
```

Naming convention: one test file per service / blueprint
(`test_<module_name>.py`). Shared fixtures live in `conftest.py`.

The repo has **no automated CI pipeline**; tests are run by
contributors and reviewers locally and through the `review-pr` /
`security-review` skills.

---

## Naming conventions

- **Snake-case** for Python modules and functions, **kebab-case**
  for shell scripts and systemd unit files.
- **`_underscore_prefix`** marks "module-private" — by convention,
  not enforced. Tests freely import these when necessary.
- **`__double-underscore-bracketed__`** placeholders are
  template-substitution targets. Never appear in deployed files.
- **Constants in UPPER_SNAKE_CASE.** Exposed to tests by importing
  the module.
- **Service files end in `_service.py`**, blueprint files do not
  (e.g., `lock_chime_service.py` vs `lock_chimes.py` blueprint).
- **Database file names** end in `.db` (`geodata.db`,
  `cloud_sync.db`).
- **Image files** are protected (`*.img` in `installation.mount_dir`).

---

## What lives where for common tasks

| You want to…                                    | Look in                                                    |
|-------------------------------------------------|------------------------------------------------------------|
| Add a new web page                              | New blueprint in `scripts/web/blueprints/` + template       |
| Add a new background worker                     | New service in `scripts/web/services/`, register in app factory |
| Add a new config key                            | `config.yaml` (with default) + `scripts/config.sh` if used in Bash |
| Add a new database table                        | `mapping_migrations.py` (new schema version + migration)   |
| Add a new SEI-extracted field                   | `sei_parser.py` + `dashcam_pb2.py` regen if proto changes  |
| Change a deployed systemd unit                  | Source template under `templates/`, then `setup_usb.sh`    |
| Change which dirs the file watcher watches      | `file_watcher_service.py::_classify_paths()`               |
| Change how a video is classified for /jobs      | `jobs.py::_classify_clip_value()`                          |
| Add a new failure pattern recommendation        | `jobs.py::_RECOMMENDATION_RULES`                           |

---

## Source files

This document is a navigational reference only. Update it whenever
you add a new top-level folder, blueprint, or service module.
