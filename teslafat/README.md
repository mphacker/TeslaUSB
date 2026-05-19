# `teslafat/` — Phase 1 design drafts (NOT BUILT)

These files are reference drafts of the `teslafat` daemon written
during the Phase B-1 design pass. They predate the Cargo workspace
at `rust/` (created in Phase 0.2) and **are not part of any build**.

The active Cargo workspace lives at `rust/`. Drafts here get ported
into `rust/crates/teslafat/src/` increment-by-increment as each
Phase 1 step (1.1 through 1.7) lands per `docs/00-PLAN.md`. Each
ported file deletes the corresponding draft on the way in, so this
directory shrinks to nothing by the end of Phase 1.

| Draft file                      | Ported by  | Notes                                                |
|---------------------------------|------------|------------------------------------------------------|
| `src/main.rs`                   | Phase 1.1  | Clap CLI, tracing init, config loader, NBD bootstrap |
| `src/config.rs`                 | Phase 1.1  | TOML config loader (drafts use YAML; will switch)    |
| `src/nbd/mod.rs`                | Phase 1.5  | Per-connection serve loop + dispatch                 |
| `src/nbd/handshake.rs`          | Phase 1.3  | NBD newstyle handshake (1:1 port, add round-trip test) |
| `Cargo.toml`                    | n/a        | Superseded by `rust/crates/teslafat/Cargo.toml`      |

> **Do not edit these drafts.** If a design change is needed,
> capture it in the next Phase 1 increment that ports the affected
> file. Editing drafts in place defeats the per-increment review
> gate.

Charter §"No code smells" forbids dead code. These files are
classified as **design drafts pending port**, not dead code; this
README documents that classification. Once Phase 1.7 lands, this
entire directory is `git rm`'d.
