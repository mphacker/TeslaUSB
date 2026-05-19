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
| ~~`src/main.rs`~~ (deleted)     | Phase 1.1  | Ported in commit landing inc-1.1; stripped to bootstrap (CLI + tracing + TOML config + sentinel). NBD/IPC wiring deferred to 1.3/1.5. |
| ~~`src/config.rs`~~ (deleted)   | Phase 1.1  | Ported in commit landing inc-1.1; schema preserved, YAML → TOML, validation reused. |
| `src/nbd/mod.rs`                | Phase 1.5  | Per-connection serve loop + dispatch                 |
| ~~`src/nbd/handshake.rs`~~ (deleted) | Phase 1.3 | Ported in commit landing inc-1.3; decomposed into pure encode/decode helpers + generic async `run` shell. Crate split into lib + bin so the protocol is reachable from unit tests without a real socket. |
| `Cargo.toml`                    | n/a        | Superseded by `rust/crates/teslafat/Cargo.toml`      |

> **Do not edit these drafts.** If a design change is needed,
> capture it in the next Phase 1 increment that ports the affected
> file. Editing drafts in place defeats the per-increment review
> gate.

Charter §"No code smells" forbids dead code. These files are
classified as **design drafts pending port**, not dead code; this
README documents that classification. Once Phase 1.7 lands, this
entire directory is `git rm`'d.
