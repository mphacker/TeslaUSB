# Copilot instructions — TeslaUSB

Binding working notes for any Copilot agent (CLI, cloud, code review) on this
repo. The code-quality charter (`docs/03-CODE-QUALITY-CHARTER.md`) wins on any
conflict; this file adds the operator directives below.

## Rust + TS only — no Python, ever (binding)

B-1 is **Rust** (daemons: `gadgetd`, `scannerd`, `indexd`, `webd`, `retentiond`,
`uploadd`, `wifid`) plus the **preact/TypeScript SPA** under `spa/`. **No Python**
in the shipped solution or the build/deploy surface — no runtime, Flask, Jinja,
gunicorn, or `.py` file.

The legacy **v1 app (`teslausb_web`, Flask) is REFERENCE ONLY.** Goal: re-create
v1's features, capabilities, and look-and-feel in Rust/TS, faster and with zero
clip loss. You MAY read v1 to recover an authoritative Tesla path, folder name,
or validation rule, and port the *behavior* idiomatically. You MUST NOT copy v1
Python (verbatim or line-translated) or reintroduce any Python.

## Builds — podman only, never local WSL (binding)

All cross-builds run through **podman on the Windows host** (debian:bookworm,
`gcc-aarch64-linux-gnu` cross linker, target `aarch64-unknown-linux-gnu`,
toolchain 1.85.0). Podman is installed (`podman.exe`, `podman-machine-default`).
**Do not** drop to local WSL (slow, not reproducible).

### Critical gotcha — invoke podman from PowerShell, not WSL bash (saves trial-and-error)

`release/build-release.sh --cross-podman` **fails on this host** because the only
`bash` is **WSL** (`bash --version` → `x86_64-pc-linux-gnu`, paths are `/mnt/c/...`):
the script's `command -v podman` doesn't find the Windows `podman.exe` inside WSL
(→ `ERROR: podman not found`), and even if shimmed, podman.exe bind mounts need
**Windows paths** (`C:\...`), not WSL `/mnt/c/...` paths. So:

- **Run the container recipe directly via `podman.exe` from PowerShell** with
  `C:\...` bind-mount sources. This is the documented "mirror the container
  recipe" path and is the fast, reliable way here.
- Reuse the **warm named volumes** so rebuilds are ~seconds, not minutes:
  `teslausb-cargo-target`, `teslausb-cargo-home`, `teslausb-rustup` (cross-build);
  `teslausb-test-target` + `teslausb-cargo-home` (tests).
- **Build only the changed crates** with `-p <crate>` (e.g. `-p webd -p schedulerd`).
- If you pipe a host-authored `.sh` into the container, **strip CR first**
  (`tr -d '\r' < script.sh | bash`) — Windows-created files are CRLF and bash
  chokes on `\r`.

**Canonical cross-build (aarch64 bins) — PowerShell, warm volumes:**
```powershell
$repo = (Get-Location).Path           # C:\...\TeslaUSB
$out  = "$repo\release\.build\aarch64-bin"   # holds bin/<crate>
podman run --rm `
  --mount "type=bind,source=$repo,target=/src,ro" `
  --mount "type=bind,source=$out,target=/out" `
  --mount "type=volume,source=teslausb-cargo-target,target=/cargo-target" `
  --mount "type=volume,source=teslausb-cargo-home,target=/root/.cargo" `
  --mount "type=volume,source=teslausb-rustup,target=/root/.rustup" `
  docker.io/library/debian:bookworm bash -lc "tr -d '\r' < /out/build.sh | bash"
```
where `/out/build.sh` mirrors `build-release.sh`'s inner recipe: apt-install
`build-essential pkg-config gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu file`,
rustup 1.85.0 + `rustup target add aarch64-unknown-linux-gnu`, copy `/src/rust`
to `/work/rust`, then
`export CARGO_TARGET_DIR=/cargo-target`,
`export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc`,
`export CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc`,
`cargo build --release --target aarch64-unknown-linux-gnu -p <crates>`, assert each
output is aarch64 (`aarch64-linux-gnu-readelf -h | grep AArch64`), `install -m0755`
to `/out/bin/<crate>`, and `sha256sum` it. First cold run does the apt+rustup
install into the volumes; subsequent runs skip it.

**Canonical test recipe (host-arch unit tests) — PowerShell, warm volumes:**
```powershell
podman run --rm -v "${PWD}:/work" `
  -v teslausb-cargo-home:/cargo-home -v teslausb-test-target:/test-target `
  -e CARGO_HOME=/cargo-home -e CARGO_TARGET_DIR=/test-target `
  -w /work/rust docker.io/library/rust:1.85-bookworm `
  bash -c "cargo test -p teslausb-core -p schedulerd -p webd"
```
(Unix-socket crates don't build on Windows host cargo — use the container.)

For a full signed/manifested release artifact, `release/build-release.sh` is still
the source of truth for the staging/manifest/verify steps; run it where a Linux
`bash` *and* a PATH-visible `podman` coexist (its `--cross-podman` step is the same
recipe above). For a one-off scoped binary deploy, the direct podman call is enough.

## Model division of labor (binding)

The orchestrator is **Claude Opus 4.8**; it owns the session and routes work:

- **Plan / break-down / decide → Opus 4.8 (orchestrator).** Frames problems,
  designs the approach, owns `todos`/`todo_deps`/`plan.md`, sequences
  dependencies, and makes the final reconciled call. Opus does not delegate
  planning.
- **Write code → `gpt-5.3-codex` (background sub-agent).** All
  substantive implementation (features, multi-file changes, porting v1 behavior)
  is delegated with a self-contained prompt: exact files, the contract, the
  constraints (this file + charter), and the acceptance tests to pass. Opus may
  make only trivial/surgical edits directly. (Superseded `mai-code-1-flash-internal`
  on 2026-06-18 by operator directive — mai produced unreliable self-reported
  verification and unscoped workspace-wide reformatting; do NOT use mai for code.)
- **Review → `gpt-5.5` (background sub-agent).** The single reviewer of record
  for adversarial reviews, second opinions, and pre-deploy plan reviews.

Delegation routes work, not judgment: Opus verifies the coder's diff (builds/tests/
reads it) and reconciles GPT-5.5's findings against the artifact rather than
rubber-stamping them.

## Implementation workflow — `docs/status.md` is the driver (binding)

`docs/status.md` is the single source of truth for what remains to reach parity
with `docs/Requirements.md`. Work it one item at a time through this loop; Opus
runs the loop and routes each step per "Model division of labor":

1. **Select** the next unchecked `[ ]` item from `status.md`. Respect its gates
   and the recommended build order — never start an item whose dependency
   (`gated:F1/F3/C1/…`) is unmet; prefer the foundation slice before features.
   Tier-C (operator/hardware-only) items are not started autonomously.
2. **Plan (Opus).** Design the implementation and break it into verifiable tasks
   (`todos`/`todo_deps`). **Check for an existing spec/task/ADR first** and
   validate it still aligns with the open item; **if it has drifted, fix the
   spec/task before coding.** Write one if none exists.
3. **Implement (GPT-5.3-codex).** Delegate the code to a `gpt-5.3-codex`
   sub-agent with the acceptance tests it must make pass.
4. **Review (GPT-5.5).** Adversarially review the coder's diff; reconcile findings;
   **send issues back to the coder and re-review until clean** (bounded — escalate to
   the operator if it doesn't converge in a few cycles).
5. **Validate by test.** Unit/integration for logic; **Playwright for any UI
   change** (see below); the hardware-test skill for device behavior. A box is
   checked only after a tested-successful run.
6. **Update `status.md`.** Tick `[x]`, link the evidence (Playwright report /
   `files/hw-results.md` / test name), and commit the status update with the change.

### Parallelism — max throughput, zero collisions

Run as many items in parallel as can proceed **without collision or rework**:

- **Partition by non-overlapping surface.** Parallelize only items whose
  file/crate/module surfaces don't overlap (e.g. one SPA screen vs. a
  `retentiond` loop vs. a docs edit). If two would touch the same files (same
  module, same screen, the gadgetd handoff state machine, a shared contract),
  **serialize them.**
- **Gates are hard ordering.** Never parallelize an item with the foundation it
  is `gated:` on; encode this in `todo_deps`.
- **One writer per shared artifact.** `status.md`, `plan.md`, and each spec/
  contract have a single writer; Opus serializes edits and merges sub-agent
  results.
- **One self-contained coder lane per item**, each with its own files + tests;
  reviews fan out to GPT-5.5 per lane. Opus tracks lanes (`lanes`/`todos`),
  reconciles, and updates `status.md` once per completed item.
- **When unsure whether two items collide, assume they do and serialize.**

## Problem-solving — mandatory parallel GPT-5.5 second opinion

For any **non-trivial design decision or issue** (bug, regression, architecture
call), don't rely solely on your own analysis: in parallel, launch a `gpt-5.5`
sub-agent with a self-contained prompt (symptoms, relevant files, constraints,
the specific question — it's stateless) to independently reach its own
conclusion while you form yours. Then **reconcile**: surface your view, GPT-5.5's
view, and the reconciled conclusion so the operator sees the reasoning; treat
disagreement as a reason to dig deeper. Re-check the final fix/plan with GPT-5.5
before anything risky (live-hardware or recording-critical). Any non-trivial
code in the fix is implemented by GPT-5.3-codex, then reviewed by GPT-5.5.

## UI work — Playwright verification is non-optional (binding)

Any change affecting the rendered SPA (preact components/screens under `spa/`,
styles, `webd` API payloads the UI consumes — anything served on
`cybertruckusb.local`) must be verified end-to-end with Playwright before it is
"done". "Tests pass" and "endpoint returns 200" are not sufficient. Extend the
existing UAT suite under `spa/test/uat/` rather than starting from scratch.

For every UI-affecting change:

1. **Drive the real page** (headless Chromium against `http://cybertruckusb.local/…`
   when deployed, or a local `webd` + SPA dev server) — confirm the browser runs
   the JS, calls the expected `webd` endpoints, and renders.
2. **Assert on perf:** TTFB, DOMContentLoaded, first-contentful-paint, and
   per-request elapsed time; surface the slowest 5–10 requests. >~2 s to
   interactive on the Pi is not "fast" — keep iterating.
3. **Assert on console:** subscribe to `page.on("console")` / `page.on("pageerror")`;
   any error/warning/pageerror is a failure unless explicitly justified.
4. **Visually verify:** screenshot at mobile (375px) and desktop (≥1280px) and
   confirm the change actually renders — don't trust DOM-only assertions when the
   bug could be CSS/z-index/layout.
5. **Verify the wiring:** prove the changed module is actually loaded by the page
   (inspect the network waterfall and `<script>`/bootstrap state) — editing a
   module the page never loads is a real failure mode here.
6. **Report:** before/after timings, the network-request table, the console log,
   and the screenshot path.
