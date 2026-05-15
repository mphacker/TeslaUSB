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

All three flows ultimately produce the same encrypted `cloud_provider_creds.bin` and the same `[teslausb]` rclone section at sync time, so the cloud archive worker, Live Event Sync, and the connection-test button all work identically regardless of provider type.

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
3. The **Live Event Sync** subsystem (if enabled) inherits NAS support automatically — no separate setup.
