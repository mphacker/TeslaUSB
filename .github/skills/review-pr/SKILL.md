---
name: review-pr
description: >
  Review GitHub pull requests for code quality, architecture compliance, and project
  conventions. Use when asked to review a PR, review pull requests, check a PR, audit PR changes,
  or give feedback on a pull request. Handles single or multiple PRs by number. Posts review
  comments directly on the PR via the GH CLI. Security review is delegated to the
  `security-review` skill — invoked automatically as part of every PR review.
---

# Pull Request Review

Perform a comprehensive review of one or more GitHub pull requests on the TeslaUSB
repository. The review covers mount safety, configuration conventions, subprocess security,
path traversal prevention, image gating, Flask blueprint patterns, resource efficiency,
and template deployment correctness — then posts the review directly on the PR via the
GH CLI. Security review is handled by the dedicated `security-review` skill, invoked in
**changed** mode against the PR's changed files.

Read `.github/copilot-instructions.md` first to load all project conventions before reviewing.

---

## Phase 0 — Prerequisites

### GH CLI Authentication

Verify the GH CLI is authenticated:

```bash
gh auth status
```

If not authenticated, stop and inform the user to run `gh auth login`.

### Load Project Conventions

Read `.github/copilot-instructions.md` in full to load all project-specific conventions,
architecture rules, mount/namespace safety patterns, configuration system, template
deployment, and feature gating patterns. These are enforced throughout the review.

---

## Phase 1 — Scope Resolution

Determine which PRs to review based on the user's request.

| Mode | Trigger examples | Behavior |
|------|-----------------|----------|
| **single** | "#42", "PR 42", "review PR 42" | One specific PR |
| **multiple** | "PRs 42, 55, 68", "review 42 55 68" | Multiple PRs by number |

### Fetch PR metadata

For each PR number, fetch metadata:

```bash
gh pr view <number> --repo mphacker/TeslaUSB --json number,title,body,headRefName,baseRefName,additions,deletions,changedFiles,labels,author,state,reviewDecision,reviews
```

**Validation checks before proceeding:**

- If the PR is already **merged** or **closed**, skip it and note the status.
- If the PR number does not exist, report the error and skip it.

### Present PR summary table

| # | Title | Author | Base ← Head | Files | +/- |
|---|-------|--------|-------------|-------|-----|
| 42 | Add map-based video browser | @dev | main ← feat/24-map-browser | 8 | +340 / -85 |

For **multiple** PRs, process each PR sequentially through Phases 2–5, then produce a
consolidated Phase 6 report.

---

## Phase 2 — Fetch PR Diff & Changed Files

### Get the list of changed files

```bash
gh pr diff <number> --repo mphacker/TeslaUSB --name-only
```

### Get the full diff

```bash
gh pr diff <number> --repo mphacker/TeslaUSB
```

### Read changed files for full context

For each changed file in the diff, read the **full current content** of the file from the
PR's head branch to understand context beyond just the diff hunks.

Group changed files by component for phased analysis:

| Component | File patterns |
|-----------|--------------|
| Flask blueprints | `scripts/web/blueprints/*.py` |
| Backend services | `scripts/web/services/*.py` |
| Templates / HTML | `scripts/web/templates/*.html` |
| Static assets | `scripts/web/static/**` |
| Configuration | `config.yaml`, `scripts/config.sh`, `scripts/web/config.py` |
| Shell scripts | `scripts/*.sh`, `*.sh` |
| Setup / deployment | `setup_usb.sh`, `templates/*` |
| Systemd / services | `templates/*.service`, `templates/*.timer` |
| Tests | `tests/*` |

### Determine applicable review phases

Not all review phases apply to every PR. Based on the changed files, determine which
phases to execute:

| Condition | Phase to execute |
|-----------|-----------------|
| Any Python file changed | Code quality & Flask patterns |
| Any `.sh` file changed | Shell script safety |
| `config.yaml` or config wrappers changed | Configuration compliance |
| Files under `services/` that do mount/umount ops | Mount & gadget safety |
| Files that handle user-supplied paths or filenames | Path traversal prevention |
| Files with `subprocess.run` or `subprocess.Popen` | Subprocess security |
| New blueprint or routes added | Image gating & nav integration |
| Files under `templates/` or `scripts/` (source templates) | Template placeholder compliance |
| Files affecting Pi resource usage (threading, memory, I/O) | Resource efficiency |
| Always | Security review (via `security-review` skill) |

---

## Phase 3 — Code Review

Apply the review checklist below to the changed files. **Review only the changed files**
— do not review unrelated code.

### Review focus areas

#### 1. Configuration Compliance

| Check | What to verify | Severity |
|-------|---------------|----------|
| No hardcoded paths | Paths read from `config.yaml` via `config.sh` or `config.py` | 🔴 Critical |
| No hardcoded credentials | Passwords, keys, ports come from config, not source | 🔴 Critical |
| Config wrapper usage | Bash uses `config.sh`; Python uses `config.py` | 🟡 Warning |
| New config values | Added to `config.yaml` with sensible defaults | 🟡 Warning |
| Both wrappers updated | If value used in both Bash and Python, both wrappers updated | 🟡 Warning |

#### 2. Mount & Gadget Safety

| Check | What to verify | Severity |
|-------|---------------|----------|
| `nsenter` wrapping | All mount/umount/mountpoint commands use `sudo nsenter --mount=/proc/1/ns/mnt` | 🔴 Critical |
| Sync operations | `sync` called before and after critical filesystem changes | 🔴 Critical |
| Unbind order | When leaving present mode: unbind UDC → remove gadget config → unmount → detach loops | 🔴 Critical |
| Quick-edit cleanup | All code paths (including exceptions) restore RO mount and LUN backing | 🔴 Critical |
| Lock file usage | Quick-edit operations use `.quick_edit_part2.lock` with stale timeout | 🟡 Warning |
| Loop device handling | RO loop devices not mounted RW; LUN backing cleared before detach | 🟡 Warning |

#### 3. Path Traversal Prevention

| Check | What to verify | Severity |
|-------|---------------|----------|
| Filename sanitization | User-supplied filenames use `os.path.basename()` | 🔴 Critical |
| Directory containment | Resolved paths verified with `os.path.commonpath()` against allowed root | 🔴 Critical |
| Extension validation | File uploads only accept expected extensions | 🟡 Warning |
| No `..` components | Path components checked for traversal attempts | 🔴 Critical |

#### 4. Subprocess Security

| Check | What to verify | Severity |
|-------|---------------|----------|
| List-based arguments | `subprocess.run()` uses list args, not string with `shell=True` | 🔴 Critical |
| No f-string commands | No `f'command {user_input}'` passed to shell | 🔴 Critical |
| Timeout specified | All subprocess calls have `timeout=` parameter | 🟡 Warning |
| Error handling | Subprocess failures caught and logged appropriately | 🟡 Warning |

#### 5. Image Gating & Feature Availability

| Check | What to verify | Severity |
|-------|---------------|----------|
| `before_request` guard | New blueprints depending on disk images have `@bp.before_request` hook | 🔴 Critical |
| Nav item guard | New nav links wrapped in `{% if <flag> %}` in `base.html` (both desktop + mobile) | 🔴 Critical |
| Availability function | New features added to `partition_service.get_feature_availability()` | 🟡 Warning |
| AJAX vs redirect | AJAX requests get 503 JSON; normal requests redirect with flash message | 🟡 Warning |

#### 6. Flask Blueprint & Service Patterns

| Check | What to verify | Severity |
|-------|---------------|----------|
| Separation of concerns | Business logic in services, not route handlers | 🟡 Warning |
| Mode-aware file ops | File operations go through services that choose RO/RW paths | 🟡 Warning |
| Samba cache invalidation | After edits in edit mode, `close_samba_share()` and `restart_samba_services()` called | 🟡 Warning |
| Error responses | Routes return appropriate HTTP status codes; AJAX gets JSON errors | 🟡 Warning |
| Logging | Appropriate log levels (WARNING for recoverable, ERROR for failures) | 🔵 Info |

#### 7. Template & Deployment Compliance

| Check | What to verify | Severity |
|-------|---------------|----------|
| Placeholder usage | Source templates use `__GADGET_DIR__`, `__MNT_DIR__`, `__TARGET_USER__`, etc. | 🔴 Critical |
| No hardcoded installed paths | Templates don't reference `/home/pi/...` directly | 🔴 Critical |
| Setup script awareness | Changes to templates/scripts will be deployed by `setup_usb.sh` | 🔵 Info |

#### 8. Shell Script Safety

| Check | What to verify | Severity |
|-------|---------------|----------|
| Error handling | `set -euo pipefail` at script top | 🟡 Warning |
| Variable quoting | All variable expansions double-quoted (`"$VAR"` not `$VAR`) | 🟡 Warning |
| Config sourcing | Values read from `config.sh`, not hardcoded | 🟡 Warning |
| eval safety | Any `eval` usage properly quotes values to prevent injection | 🔴 Critical |

#### 9. Resource Efficiency (Pi Zero 2 W)

| Check | What to verify | Severity |
|-------|---------------|----------|
| Memory management | No unbounded data structures; lazy imports for heavy libs (av, PIL) | 🟡 Warning |
| Concurrency limits | Thumbnail generation limited; no unbounded thread spawning | 🟡 Warning |
| I/O efficiency | Large files streamed (not loaded into memory); chunked uploads | 🟡 Warning |
| Background tasks | Long operations run in background threads, not blocking requests | 🔵 Info |

#### 10. Power-Loss Safety

| Check | What to verify | Severity |
|-------|---------------|----------|
| Atomic writes | File writes use temp file + `os.fsync()` + `os.rename()` pattern | 🔴 Critical |
| Sync calls | `sync` called before USB gadget re-enumeration | 🟡 Warning |
| Recovery paths | Boot scripts handle incomplete operations gracefully | 🟡 Warning |

#### 11. Security Review

Invoke the `security-review` skill (`.github/skills/security-review/SKILL.md`) in
**changed** mode against the PR's changed files. The security-review skill covers
subprocess injection, path traversal, configuration security, mount safety, network
exposure, WiFi AP security, file upload validation, root privilege usage, dependency
security, and data protection. Merge its findings into the review under a **Security**
category.

### Review rules

- **Only report actual violations found.** Do not list things that passed.
- **Provide file paths and line numbers** for every finding.
- **Distinguish between new code and existing code.** Focus findings on code that was
  **added or modified** in the PR. Existing code issues may be noted as info-level but
  should not block the review.
- **Check the PR description** for context — the author may have documented intentional
  deviations or known limitations.

---

## Phase 4 — Construct Review

### Severity levels

| Severity | Meaning | Action |
|----------|---------|--------|
| 🔴 **Critical** | Mount safety violations, data corruption risk, command injection, path traversal | **Must fix** before merge |
| 🟡 **Warning** | Convention violations, missing error handling, hardcoded values | **Must fix** in this PR |
| 🔵 **Info** | Suggestions, minor improvements, latent code smells, defense-in-depth | **Must fix** in this PR (or document deferral with follow-up issue link) |

**TeslaUSB policy: zero code smells or latent issues left behind.** Info findings are
NOT optional. They represent latent code smells or defense-in-depth gaps that should be
addressed in the same PR — leaving them unfixed accumulates technical debt and erodes
the code quality bar. The only acceptable reasons to leave an Info finding open are:

1. It is a false positive (state why in the action items).
2. The fix requires a larger refactor out of scope; **a follow-up GitHub issue must be
   filed and linked** in the action items.
3. The PR author / user explicitly accepts the deferral (and the deferral is documented).

Phrase action items for Info findings as "expected to address" — never "optional" or
"nice to have."

### Review decision

| Condition | Decision | GH CLI flag |
|-----------|----------|-------------|
| Any 🔴 Critical findings | Request changes | `--request-changes` |
| Any 🟡 Warning **or** 🔵 Info findings | Comment | `--comment` |
| No findings — clean PR | Approve | `--approve` |

**Never merge a PR as part of this skill.** The review's only output is a posted review
(approve / request-changes / comment). The actual merge decision belongs to the user, not
the agent. See the "Does not merge PRs" guardrail below.

### Review body format

Build the review body as Markdown:

```markdown
## PR Review — #{number}

**Decision:** ✅ Approved / ⚠️ Changes Requested / 💬 Comment

**Summary:** N critical, M warnings, P info findings across K files.

**Phases executed:** [list of applicable phases]

### Findings

#### `path/to/file.py`

| # | Severity | Line(s) | Category | Finding |
|---|----------|---------|----------|---------|
| 1 | 🔴 | L42-L45 | Subprocess | User input passed to subprocess via f-string without sanitization |
| 2 | 🟡 | L78 | Config | Hardcoded path `/home/pi/TeslaUSB` should use `GADGET_DIR` from config |

#### `path/to/script.sh`

| # | Severity | Line(s) | Category | Finding |
|---|----------|---------|----------|---------|
| 1 | 🔴 | L12 | Mount | Missing `nsenter --mount=/proc/1/ns/mnt` wrapper on umount call |

### Action Items

1. **Critical** — [must-fix items — block merge until resolved]
2. **Warnings** — [should-fix items — fix in this PR]
3. **Info** — [expected-to-address items in this PR; cite a follow-up issue for any deferral]

<!-- skill:review-pr:review:{number} -->
```

For a clean approval:

```markdown
## PR Review — #{number}

**Decision:** ✅ Approved

Code quality, mount safety, configuration, and conventions all look good. No issues found.

<!-- skill:review-pr:review:{number} -->
```

---

## Phase 5 — Post Review via GH CLI

### Duplicate check

Before posting, check if a review from this skill already exists on the PR:

```bash
gh pr view <number> --repo mphacker/TeslaUSB --json reviews --jq '.reviews[].body' | grep -F "skill:review-pr:review:<number>"
```

Also check PR comments:

```bash
gh pr view <number> --repo mphacker/TeslaUSB --json comments --jq '.comments[].body' | grep -F "skill:review-pr:review:<number>"
```

If the marker is found, **do not post a new review**. Inform the user that a review was
already posted and show the existing review. If the user explicitly asks to re-review,
use an updated marker: `<!-- skill:review-pr:re-review:{number}:{date} -->`.

### Post the review

Write the review body to a temporary file and use `--body-file`:

```powershell
$reviewBody | Out-File -FilePath "$env:TEMP\pr-review-<number>.md" -Encoding utf8

gh pr review <number> --repo mphacker/TeslaUSB --approve --body-file "$env:TEMP\pr-review-<number>.md"
# or --request-changes or --comment

Remove-Item "$env:TEMP\pr-review-<number>.md"
```

### Post inline comments (optional, for critical findings)

For 🔴 Critical findings, post inline comments at the specific file and line so the author
sees them directly in context:

```powershell
$commitId = gh pr view <number> --repo mphacker/TeslaUSB --json headRefOid --jq '.headRefOid'

gh api "repos/mphacker/TeslaUSB/pulls/<number>/comments" `
  --method POST `
  -f "body=🔴 **Critical:** <finding description>" `
  -f "path=<file-path>" `
  -f "commit_id=$commitId" `
  -f "line=<line-number>" `
  -f "side=RIGHT"
```

**Rules for inline comments:**
- Only post inline comments for 🔴 Critical findings.
- Maximum 10 inline comments per PR to avoid noise.
- Each inline comment should be self-contained and actionable.
- The main review body already contains all findings — inline comments are supplementary.

---

## Phase 6 — Report

After all PRs have been reviewed, produce a final summary.

### Single PR

If only one PR was reviewed, the review body posted in Phase 5 serves as the report.
Confirm the review was posted successfully:

```
✅ Review posted on PR #42 — Changes Requested (3 critical, 2 warnings, 1 info)
```

### Multiple PRs

Produce a consolidated summary table:

| # | Title | Decision | Critical | Warning | Info |
|---|-------|----------|----------|---------|------|
| 42 | Add map-based video browser | ⚠️ Changes Requested | 3 | 2 | 1 |
| 55 | Fix chime upload validation | ✅ Approved | 0 | 0 | 0 |
| 68 | Update WiFi roaming config | 💬 Comment | 0 | 1 | 3 |

**Totals:** 3 PRs reviewed, 1 approved, 1 changes requested, 1 commented.

List any PRs that were skipped (merged, closed, not found) with the reason.

---

## Guardrails

### What this skill does

- Reviews PR diffs against project-specific conventions from `.github/copilot-instructions.md`
- Validates mount safety, configuration compliance, path security, subprocess safety,
  image gating, template deployment, resource efficiency, and power-loss safety
- Invokes the `security-review` skill in changed mode for security assessment of PR files
- Posts structured reviews directly on the PR via GH CLI
- Posts inline comments for critical findings
- Handles single or multiple PRs in one invocation

### What this skill does NOT do

- **Does not auto-fix code.** This is a review — it reports findings for the author to fix.
- **Does not run tests.** TeslaUSB has no automated test suite.
- **Never merges PRs — under any circumstances.** The review decision is advisory only.
  Even on a clean Approve, the skill MUST NOT call `gh pr merge`, push to `main`, or
  otherwise combine the PR into the base branch. Merging is exclusively the user's
  decision and must be performed by the user (or by an explicit, separate instruction
  outside this skill). If the user asks the agent to merge after the review, that is a
  separate task — confirm with the user first and treat it as a deliberate, supervised
  operation, never as the implicit next step after a review.
- **Does not treat Info findings as optional.** Info-level items are latent code smells
  / defense-in-depth gaps and are expected to be addressed in the same PR. If the
  reviewer finds genuine deferrals are needed, a follow-up GitHub issue must be filed
  and linked in the review.
- **Does not review files outside the PR diff.** Stay within the changed files.
- **Does not auto-select PRs.** The user must provide PR numbers.
- **Does not deploy changes.** Deployment via `setup_usb.sh` is a separate step.

### False positive avoidance

- Before flagging a hardcoded path, **check if it's a system path** (e.g., `/proc/1/ns/mnt`,
  `/sys/kernel/config`) that cannot come from config — these are valid.
- Before flagging missing `nsenter`, **check if the operation is already inside an nsenter
  context** (e.g., called from within a script that wraps its operations).
- Before flagging missing image gating, **check if the blueprint is intentionally ungated**
  (e.g., `fsck.py` is API-only with no nav link and handles missing images internally).
- **Read the PR description and comments** — the author may have documented intentional
  deviations or known trade-offs.
- **System directories are not config values** — `/proc`, `/sys`, `/dev`, `/run` paths
  are kernel interfaces and should not come from config.yaml.
