# Main-to-B-1 migration destination contract

## Purpose

This contract defines where supported legacy settings belong before a
converter is implemented. Discovery may report source keys, but conversion
must not guess a destination or write an unmanaged configuration file.

## Ownership map

| Legacy section | B-1 destination owner | Migration behavior |
| --- | --- | --- |
| `installation` | setup/system unit environment | Map supported path and user values; reject host-specific paths |
| `disk_images` | gadgetd provisioning contract | Preserve image names as a report; never rewrite or resize images automatically |
| `setup` | installer dry-run report | Report sizing requests; require explicit operator approval for provisioning |
| `network` | webd/wifid configuration | Ignore passwords and secret keys; validate only non-secret port values |
| `offline_ap` | wifid configuration | Convert only validated non-secret network parameters; quarantine unsupported values |
| `system` | installer/platform policy | Report paths; do not modify boot or Samba files during conversion |
| `web` | webd/SPA settings | Convert bounded numeric/UI settings; generate new B-1 secrets separately |
| `mapping` | indexd configuration | Convert validated sampling and threshold values; preserve unknown keys in report |
| `cloud_archive` | indexd/uploadd/retentiond contracts | Convert policy values only after provider and queue contracts are present |

## Conversion output

A dry-run converter will emit a versioned report containing:

- source discovery schema and destination schema;
- supported mappings with normalized values;
- unknown or rejected keys by relative section;
- secret-key omissions (without values);
- required operator decisions;
- files that would be created, without writing them;
- explicit destructive-operation blockers.

The first implementation steps of this contract are read-only commands:

- `teslausb-migrate plan --root <path>` maps detected top-level sections to
  destination owners and lists unknown sections for quarantine.
- `teslausb-migrate convert --root <path>` performs a dry-run conversion of
  supported non-secret scalar settings, emits normalized values, and
  quarantines unknown/unsafe settings.

Neither command writes destination files.

### Current supported scalar mappings (dry-run)

| Legacy setting | Destination owner | Destination key | Validation |
| --- | --- | --- | --- |
| `installation.user` | setup/system unit environment | `system_user` | lowercased ASCII user, 1-32 chars, `[a-z0-9_-]+` |
| `setup.archive_size_gb` | installer dry-run report | `archive_size_gb` | integer 16-8192 |
| `network.http_port` | webd/wifid configuration | `http_port` | integer 1-65535 |
| `offline_ap.enabled` | wifid configuration | `enabled` | boolean (`true/false/yes/no/on/off/1/0`) |
| `offline_ap.channel` | wifid configuration | `channel` | integer 1-165 |
| `web.port` | webd/SPA settings | `port` | integer 1-65535 |
| `web.session_timeout_minutes` | webd/SPA settings | `session_timeout_minutes` | integer 1-1440 |
| `mapping.sample_seconds` | indexd configuration | `sample_seconds` | integer 1-300 |
| `mapping.sentry_speed_mph` | indexd configuration | `sentry_speed_mph` | integer 0-120 |
| `cloud_archive.reserve_gb` | indexd/uploadd/retentiond contracts | `reserve_gb` | integer 0-4096 |
| `cloud_archive.max_retries` | indexd/uploadd/retentiond contracts | `max_retries` | integer 0-20 |

All other keys, unknown sections, non-scalar values, and invalid values are
quarantined in the report.

It must not contain passwords, tokens, Flask secret keys, private paths, or
raw credential-file contents.

## Safety and idempotence

- Conversion is read-only until an explicit apply command is designed.
- `disk.img`, archive media, mapping databases, and credential files are never
  modified by conversion.
- Unknown settings are quarantined in the report rather than silently dropped.
- A repeated dry run against unchanged inputs produces the same normalized
  report.
- Apply, verification, rollback, and restart sequencing remain separate
  contracts.

## Acceptance

- Every supported legacy section has exactly one B-1 owner.
- Secret-bearing settings are excluded from normalized values.
- Unsupported values produce explicit blockers or quarantine entries.
- No converter implementation begins until each destination owner has a
  versioned wire/config contract.
