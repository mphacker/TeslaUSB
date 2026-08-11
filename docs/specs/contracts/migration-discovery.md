# Main-to-B-1 migration discovery contract

## Purpose

Discovery is a read-only inspection of a legacy TeslaUSB installation. It
reports what was found and what would block migration. It never edits files,
rewrites configuration, changes credentials, touches the catalog, or restarts
services.

## Inputs

The discovery command receives an optional legacy root directory. If omitted,
the installer supplies the known installation root. Discovery must inspect only
paths beneath that root after canonicalizing it.

The initial inventory includes:

- `config.yaml`;
- legacy mapping databases and their schema/version markers;
- archive and media roots;
- credential/configuration files;
- `disk.img` and other car-facing storage artifacts;
- service or systemd state when readable.

The initial service inventory includes the legacy TeslaUSB service unit and
common web, uploader, and Wi-Fi unit names beneath
`etc/systemd/system/`. Unknown service files are not executed or parsed as
configuration; they remain outside the bounded discovery inventory.

Missing paths are normal findings, not errors.

## Output

The result is a versioned JSON report:

```json
{
  "schema": 1,
  "legacy_root": "redacted",
  "installation_detected": true,
  "items": [
    {
      "kind": "config",
      "relative_path": "config.yaml",
      "present": true,
      "bytes": 1234,
      "modified_at": 0,
      "sha256": null
    }
  ],
  "config_keys": ["mode"],
  "blockers": [
    {
      "code": "unsupported_mapping_schema",
      "severity": "error",
      "message": "The mapping database schema is not recognized."
    }
  ]
}
```

Absolute paths, credential contents, token values, and passwords must not be
returned. Hashes are optional and must be explicitly requested because hashing
large archive trees can be slow.
`config_keys` contains only bounded top-level YAML key names; configuration
values are never returned. Value normalization is handled separately by the
read-only `teslausb-migrate convert --root <path>` dry-run command defined in
`migration-destination.md`.

## Safety rules

- Discovery is read-only and must not shell out to legacy scripts.
- All paths are canonicalized beneath the supplied root.
- File sizes, item counts, and report sizes are bounded.
- Permission errors are reported as findings; they are not silently treated as
  “missing.”
- An apply/migrate operation is not implied by a successful discovery report.

## Acceptance

- Running discovery twice produces the same report when the installation is
  unchanged.
- A missing config, unreadable credential file, unknown mapping schema, and
  absent archive root each produce an explicit finding.
- No discovery test changes its fixture files.
- The report contains no credential contents or absolute host paths.
