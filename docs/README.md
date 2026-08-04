# TeslaUSB B-1 docs

This directory holds the **B-1 (Rust/TS) architecture specs and contracts**.

## ⚠️ Docs-provenance note (read this first)

The B-1 rewrite lives on the `b1-clean` branch. Its crates reference a set of
spec/contract paths in their module docs — e.g. `docs/specs/uploadd.md`,
`docs/specs/contracts/single-writer-lease.md`,
`docs/specs/contracts/wifi-upload-throttle.md`,
`docs/specs/contracts/indexd-schema.md` (D1). **Historically those files were
authored but never committed to `b1-clean`**, so the links dangled. The v1
user-facing docs (`CLOUD_ARCHIVE.md`, `ARCHITECTURE.md`, …) live only on the
`main` branch, which is the **v1 Python/Flask reference app** — not the B-1
rewrite.

This `docs/` tree is being re-established on `b1-clean`, one feature at a time,
as each feature is specced. The **cloud-sync** specs below are the first
tranche (authored 2026-07 for the cloud-sync buildout). The remaining
code-referenced contracts (D1 indexd-schema, D3 single-writer-lease, D4
wifi-upload-throttle) describe **already-shipped** subsystems; where a cloud
spec depends on one, the relevant shape is restated inline and marked
`(derived from code)` until the owning contract is back-filled.

## Cloud-sync specs (this tranche)

| Doc | Owns |
|-----|------|
| [`specs/uploadd.md`](specs/uploadd.md) | The `uploadd` daemon: architecture, invariants, state machine, config, the `serve` live-wiring contract. |
| [`specs/contracts/indexd-cloud-schema.md`](specs/contracts/indexd-cloud-schema.md) | indexd migration **v6** (upload queue, dedup, stats) + the cloud control-socket RPCs. |
| [`specs/contracts/cloud-provider-creds.md`](specs/contracts/cloud-provider-creds.md) | The encrypted credential store, hardware-bound key derivation, backend allow-list, transient `rclone.conf`. |
| [`specs/contracts/webd-cloud-api.md`](specs/contracts/webd-cloud-api.md) | The `/api/cloud` HTTP surface the SPA Cloud screen consumes. |

The concept source these port is v1's
[`CLOUD_ARCHIVE.md`](https://github.com/mphacker/TeslaUSB/blob/main/docs/CLOUD_ARCHIVE.md)
(on `main`). Behavior is ported idiomatically to Rust/TS; **no Python is
reintroduced**.

These specs incorporate the decisions from a Tier-3 adversarial contract review
(audit trail: `files/cloud-p0-review-reconciliation.md`, decisions D1–D10 /
M1–M7). Two review outcomes changed scope and are called out where they land:
the unit of work is a **parent event + child objects** (not one file per event),
and **webd gains an authenticated-operator + CSRF layer** as a hard prerequisite
for the cloud mutation routes (`webd-cloud-api.md` §0) — webd has no auth today.

## Retention

| Doc | Owns |
|-----|------|
| [`specs/retention-eviction.md`](specs/retention-eviction.md) | What actually deletes archived footage: the drop-in that arms `retentiond` eviction, mode resolution, why deletion is **not** gated on cloud durability, the surviving safety gates, and the reimage hazard. |

> **Read this before making any durability or safety argument.** The shipped
> `retentiond.service` says `--no-delete`, but production overrides it and runs
> eviction **armed, with permanent loss allowed and the durability gate
> bypassed**. Earlier notes in this repo claimed the opposite.

