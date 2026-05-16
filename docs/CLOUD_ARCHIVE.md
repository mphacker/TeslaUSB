# Cloud Archive — provider setup

TeslaUSB uploads dashcam events to your cloud storage of choice via
[rclone](https://rclone.org/). This page covers the supported provider
types and how to set them up from the web UI.

## Supported providers

| Type | Backend | UI flow | Where credentials live |
|------|---------|---------|------------------------|
| **OAuth** | Google Drive, OneDrive, Dropbox | Paste an `rclone authorize` token blob from a desktop machine | OAuth refresh token |
| **S3-style** | Amazon S3, Backblaze B2, Wasabi | Inline form (Access Key / Secret Key / Region / Bucket / Endpoint) | API keys (cleartext per rclone convention) |
| **NAS / custom rclone** *(issue #165)* | SFTP, WebDAV, SMB/CIFS, FTP, S3-compatible (custom endpoint), Azure Blob, OpenStack Swift | Either a guided form or an `rclone.conf` paste | Hardware-bound Fernet-encrypted blob in `cloud_provider_creds.bin` |

All three flows ultimately produce the same encrypted `cloud_provider_creds.bin` and the same `[teslausb]` rclone section at sync time, so the cloud archive worker, the live-event upload path (`PRIORITY_LIVE_EVENT` rows in `pipeline_queue`), and the connection-test button all work identically regardless of provider type.

## NAS / Custom rclone (issue #165)

The "NAS / Custom rclone" entry in the provider dropdown is a single endpoint that covers nine rclone backend types. Two input modes are offered:

### Form mode (recommended for most NAS units)

1. Open the **Cloud** tab → **Cloud Provider** section.
2. Pick **NAS / Custom rclone** in the Provider dropdown.
3. Pick the **Backend type** that matches your storage:
   - **SFTP** — Synology, QNAP, TrueNAS, any Linux server with `sshd`.
   - **WebDAV** — Nextcloud, ownCloud, Synology WebDAV, generic.
   - **SMB / CIFS** — Windows file share, Synology / QNAP SMB.
   - **FTP** — legacy.
   - **S3-compatible** — MinIO, Ceph RGW, IDrive e2, custom endpoint.
   - **Backblaze B2 (advanced)** — direct keys.
   - **Wasabi (advanced)** — direct keys with Wasabi endpoint preset.
   - **Azure Blob Storage**.
   - **OpenStack Swift**.
4. Fill in the fields the form asks for. Required fields are marked with **\***. Hover the field for hints; see the [rclone docs](https://rclone.org/) for full semantics.
5. Click **Save & Connect**. The credentials are encrypted with the Pi's hardware-bound key and stored on the SD card; an immediate connection test is run.

### Paste mode (for users who already have an `rclone.conf`)

If you already have an existing `~/.config/rclone/rclone.conf` on a desktop machine, just copy the entire `[remote]` block and paste it into the **Paste rclone.conf** tab:

```ini
[my-nas]
type = sftp
host = nas.local
user = pi
pass = ZAlRez1m2_oEDbSn-jxvLY1eAvXzKPm6
port = 22
```

Behaviour:
- The section name (`[my-nas]` here) is **discarded** — TeslaUSB always stores remotes as `[teslausb]`.
- A `pass` field that's already obscured (rclone's standard format) is kept verbatim. A cleartext password is **not** auto-detected — paste the obscured form, or use Form mode.
- Multiple sections are rejected (avoids `crypt`/`union`/`chunker` wrap-remote attacks).
- Backend types outside the supported allow-list are rejected (see below).

### Supported backend types

Allow-listed types: `sftp`, `webdav`, `smb`, `ftp`, `s3`, `b2`, `wasabi`, `azureblob`, `swift`.

**Not supported** (and explicitly rejected at parse time):
- `crypt`, `union`, `chunker` — wrap-remote types that reference a second remote name TeslaUSB doesn't store.
- `local` — would let an attacker who gains web-UI access copy archive data to arbitrary local paths.
- `http` — read-only, useless for an upload destination.

### Choosing the bucket / folder

For S3-style backends, the **bucket name** is part of the upload path, not the rclone config. After connecting, scroll to **Sync Settings → Remote folder** and either:
- Type the bucket name (and optional sub-path), e.g. `my-teslausb-bucket/dashcam`, or
- Click **Browse** and pick a folder.

For SFTP / WebDAV / SMB / FTP, the remote folder is a path on the server, e.g. `/volume1/dashcam` or `/srv/teslausb`.

## How the credentials are stored

- **Encryption**: Fernet (AES-128-CBC + HMAC-SHA-256), with the key derived from the Pi's SoC serial + `/etc/machine-id` + a per-install random salt at `tesla_salt.bin`. The credentials cannot be decrypted on a different physical Pi.
- **At-rest format**: a single binary file at `cloud_provider_creds.bin` (atomically rewritten via temp + fsync + rename).
- **In-flight**: at sync time, the worker decrypts the creds, writes a transient `rclone.conf` to `/run/teslausb/` (tmpfs — never touches disk), runs rclone, then deletes the conf.
- **Passwords for sftp / webdav / smb / ftp** are passed through `rclone obscure` before storage so the on-disk and in-flight `rclone.conf` files never carry the cleartext.
- **S3-style secret keys** are stored verbatim — rclone does not obscure them and will not parse an obscured form.

## Verifying it works

1. After **Save & Connect**, the **Test Connection** button runs `rclone lsd teslausb:` and reports success or the rclone error verbatim.
2. The **Cloud Archive** queue starts draining the moment WiFi connects. The map page's clip overlay shows the sync icon for archived clips.
3. Sentry / Saved-event clips are uploaded with priority over the bulk catch-up backlog (they enter `pipeline_queue` at `PRIORITY_LIVE_EVENT`); no separate setup is required for the new provider types.

## Folders to sync

The Cloud Sync page exposes a **Folders to sync** checklist with three options:

| Folder           | What it contains                                                                                  | When to enable                                                                                                              |
|------------------|---------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------|
| `SentryClips`    | Per-event Tesla folders triggered by the Sentry alarm (impacts, intrusions). Each folder has 6 cameras + `event.json`. | Almost always — Sentry events are one-time, irreplaceable signals you want a permanent off-Pi copy of.                       |
| `SavedClips`     | Per-event Tesla folders created by horn-honk / on-screen save. 6 cameras + `event.json` per event.                      | Usually — these are clips you've already decided are worth keeping.                                                          |
| `ArchivedClips`  | The Pi's SD-card-resident snapshot of `RecentClips` (the rolling 1-hour buffer). Maintained by the archive subsystem. | Enable if you want continuous coverage of driving footage backed up off-Pi. Larger volume than events.                       |

> **Why isn't `RecentClips` on the list?** Tesla rotates `RecentClips` on a ~60-minute ring, so a clip the cloud worker picks up may be overwritten by Tesla before the upload finishes. The archive subsystem copies `RecentClips` to `ArchivedClips` on the SD card every 2 minutes (well inside the rotation window). Syncing `ArchivedClips` is what actually preserves driving footage long-term — syncing `RecentClips` directly would be racey and largely wasted work.

> **Backward compatibility:** if your `config.yaml` was created before PR #219 and still lists `RecentClips` under `cloud_archive.sync_folders` or `cloud_archive.priority_order`, the code silently rewrites it to `ArchivedClips` on every read. The next time you click **Save settings** in the UI, the canonical form is written back to disk.

### Priority order

The **order of the folders in the list** is also the **upload priority order** (top = first to drain). Drag a folder up or down in the configured list and the worker honours it on the next sync iteration — no restart needed. Within each folder the worker still preserves the oldest-event-first rule so the most at-risk clips drain before the newest ones.

The priority sort is computed as `folder_index * 1000 + content_score`. The `1000` multiplier is strictly larger than the max per-clip content score, so the folder axis always dominates.

## Reset counters

The dashboard at the top of the Cloud Sync page shows cumulative totals — **Events Synced**, **Events Pending**, **Failed**, and **Transferred**. The **Reset counters** button (just below the cards, on the right) zeros the cumulative totals.

| Counter             | Affected by Reset? | Why                                                                                  |
|---------------------|--------------------|--------------------------------------------------------------------------------------|
| **Events Synced**   | ✅ Yes — zeroed    | Cumulative lifetime total. Reset gives you a clean "since this date" view.            |
| **Transferred**     | ✅ Yes — zeroed    | Cumulative lifetime byte total.                                                       |
| **Events Pending**  | ❌ No — preserved  | Reflects the current queue depth. Zeroing it would lie about pending work.            |
| **Failed**          | ❌ No — preserved  | Reflects current failures that still need attention.                                  |

### What the reset is **not**

> **The reset is purely cosmetic on the dashboard. It does NOT trigger any re-uploads.** Files that were already uploaded to your cloud storage stay marked as synced internally, so the next sync pass still recognises them and skips them. The reset only changes what the *Synced* and *Transferred* dashboard numbers display.

Implementation detail (for operators who care): the reset writes the current UTC timestamp to `cloud_archive_meta.stats_baseline_at` in `cloud_sync.db`. The dashboard query then filters `cloud_synced_files.synced_at > baseline` when computing the *Synced* count and *Transferred* sum. The `cloud_synced_files` rows themselves — which are the dedup oracle — are untouched.

A confirmation dialog is shown before the reset runs. After confirming, the UI updates immediately (no need to wait for the next 10-second poll cycle).

### When to use it

- After a long period of testing different cloud providers, when you want a clean baseline.
- After a one-time bulk catch-up sync where the lifetime numbers no longer represent the steady-state rhythm of your fleet.
- Before showing the page to someone else and you'd rather not have months of accumulated totals on display.

The baseline is persisted, so the new dashboard numbers survive reboots and service restarts. The UI shows a small "Synced/Transferred counts since YYYY-MM-DD" hint near the cards once a baseline is set.
