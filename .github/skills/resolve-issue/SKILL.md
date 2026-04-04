---
name: resolve-issue
description: >
  Resolve GitHub issues by investigating root cause, implementing fixes or features, and
  running a code review. Use when asked to resolve, fix, or implement a GitHub issue, a
  specific sub-item within a large issue, all open issues, or issues filtered by label/type.
---

# Resolve GitHub Issue

Investigate, diagnose, implement, and verify fixes for GitHub issues on the TeslaUSB
repository. This skill enforces rigorous root-cause analysis with proof before any code
changes, then follows a structured implementation and review workflow.

Read `.github/copilot-instructions.md` first to load all project conventions.

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
architecture rules, mount safety patterns, configuration system, and template deployment
workflow. These are enforced throughout the implementation.

---

## Phase 1 — Scope Resolution

Determine which issues to resolve based on the user's request. Four modes, auto-detected:

| Mode | Trigger examples | Behavior |
|------|-----------------|----------|
| **single** | "#42", "issue 42", "resolve issue 42" | One specific issue |
| **sub-item** | "issue 228, work on item 1", "resolve #228 item 5" | One specific numbered item within a large issue |
| **filtered** | "all bug issues", "issues labeled enhancement" | Open issues matching a label/type filter |
| **all** | "all issues", "resolve all open issues" | Every open issue |

### Single mode

```bash
gh issue view <number> --repo mphacker/TeslaUSB --json number,title,body,labels,state,comments
```

### Sub-item mode

Use this mode when the user references both an issue number **and** a specific item ID
within that issue (e.g., "issue 228, work on item 1"). This is designed for large issues
that contain multiple numbered recommendations, tasks, or concerns.

**Step 1 — Fetch the issue:**

```bash
gh issue view <number> --repo mphacker/TeslaUSB --json number,title,body,labels,state
```

**Step 2 — Extract the target item from the issue body:**

Search the issue body for the item matching the requested ID. Items can appear in several
formats — match any of these patterns:

- Table row with bold ID: `| **{id}** |`
- Checkbox list item: `- [ ] **{id}.** ...` or `- [ ] {id}. ...`
- Numbered heading: `### {id}. ...` or `### Item {id}: ...`

If the item ID is not found in the issue body, **stop** and inform the user:

> Could not find item **{id}** in issue #{number}. Available items: [list discovered IDs].

**Step 3 — Check if item is already completed:**

If the item line contains strikethrough markers (`~~**{id}**~~`) or is a checked checkbox
(`- [x]`), the item has already been resolved. Inform the user and stop.

**Step 4 — Present the scope:**

Show the user the specific item that will be worked on:

> **Issue #{number}** — Item {id}: [item description extracted from the row/line]

**HOLD label handling:** Sub-item mode **bypasses** the HOLD label check. The user is
explicitly selecting a specific item to work on, which overrides the hold. However, still
log a note if the issue has the HOLD label.

Proceed to Phase 2. The investigation and implementation scope is limited to **only** the
selected item — do not address other items in the issue.

### Filtered mode

```bash
gh issue list --repo mphacker/TeslaUSB --state open --label "<label>" --json number,title,labels,createdAt
```

### All mode

```bash
gh issue list --repo mphacker/TeslaUSB --state open --json number,title,labels,createdAt --limit 100
```

Filter out pull requests and issues with the **HOLD** label.

If running in **single** mode, check the issue's labels before proceeding. If the issue has
a `HOLD` label, **stop immediately** and inform the user:

> Issue #N has the **HOLD** label and cannot be worked on. Remove the label when the issue
> is ready to be resolved.

Do **not** proceed to Phase 2 or any subsequent phase for issues with the `HOLD` label.

Present the resolved scope:

| # | Title | Labels | Created |
|---|-------|--------|---------|
| 42 | Video streaming fails on large files | bug | 2026-02-15 |

For **filtered** and **all** modes, process each issue sequentially through Phases 2–7.

---

## Phase 2 — Deep Investigation & Root-Cause Analysis

This phase demands rigorous, proof-driven analysis. Do not guess. Do not assume. Every
conclusion must be backed by evidence from the codebase.

### 2.1 — Understand the Issue

Read the full issue body, all comments, and any linked issues or PRs. Extract:

- **Symptom:** What is the observable problem or desired feature?
- **Reproduction steps:** If provided, note them. If not, infer them from the description.
- **Affected area:** Which feature, component, or subsystem is involved?
- **Expected vs. actual behavior:** For bugs. For features, the acceptance criteria.

### 2.2 — Codebase Exploration

Search the codebase to understand the relevant code paths. Use a combination of:

- `explore` sub-agents for broad exploration of the affected feature area
- `grep` for specific symbols, error messages, or patterns mentioned in the issue
- `view` to study the implementation of affected functions/modules

**Key areas to investigate by component type:**

| Component | Files to examine |
|-----------|-----------------|
| Web UI / Flask routes | `scripts/web/blueprints/*.py`, `scripts/web/templates/*.html` |
| Backend services | `scripts/web/services/*.py` |
| Configuration | `config.yaml`, `scripts/config.sh`, `scripts/web/config.py` |
| USB gadget / mount ops | `present_usb.sh`, `edit_usb.sh`, `scripts/web/services/partition_mount_service.py` |
| Setup / installation | `setup_usb.sh`, `templates/`, `scripts/` |
| WiFi / AP | `scripts/wifi-monitor.sh`, `scripts/web/services/ap_service.py`, `scripts/web/services/wifi_service.py` |
| Static assets / JS | `scripts/web/static/js/*.js`, `scripts/web/static/css/*.css` |

Build a **mental model** of how the affected area works before forming any hypothesis.

### 2.3 — Hypothesis Generation

Generate **all plausible hypotheses** for the root cause (for bugs) or implementation approach
(for features). For bugs, consider at minimum:

- Logic errors in the primary code path
- Missing null/edge-case handling in Python (None checks, empty strings, missing dict keys)
- Race conditions (concurrent quick_edit_part2 operations, lock file contention)
- Incorrect mount namespace handling (missing `nsenter`, wrong mount path for mode)
- Configuration errors (config.yaml values not loaded, hardcoded paths)
- Mode awareness issues (present vs edit mode, RO vs RW mount paths)
- Path traversal or filename sanitization gaps
- Subprocess failures (command not found, permission denied, timeout)
- Template placeholder expansion issues (unexpanded `__GADGET_DIR__` etc.)
- Image gating gaps (missing `before_request` guard, unchecked `os.path.isfile()`)

### 2.4 — Systematic Elimination

For each hypothesis, **gather evidence** to confirm or eliminate it:

```
| # | Hypothesis | Evidence | Verdict |
|---|-----------|----------|---------|
| 1 | Missing nsenter for mount op | partition_mount_service.py L45 uses nsenter correctly | ❌ Eliminated |
| 2 | Config value hardcoded instead of read from config.yaml | setup_usb.sh L120 uses $GADGET_DIR from config.sh | ❌ Eliminated |
| 3 | Path traversal in video streaming | videos.py L330 uses os.path.basename() but no commonpath check | ✅ Confirmed |
```

**Rules:**
- You **must** investigate at least 3 hypotheses for bugs (more for complex issues).
- Each hypothesis must have **concrete evidence** (file path, line number, code snippet)
  supporting its elimination or confirmation.
- You must **eliminate all other plausible hypotheses** before declaring a root cause.
- If multiple hypotheses survive elimination, investigate further until only one remains,
  or document that there are multiple contributing causes.

### 2.5 — Root Cause Declaration

State the confirmed root cause with:

- **What:** The specific code defect or missing implementation.
- **Where:** File path(s) and line number(s).
- **Why:** Why that code is wrong or missing — reference the expected behavior.
- **Proof:** The concrete evidence that confirms this and eliminates alternatives.

For features: state the implementation plan with the specific files/methods that need to be
created or modified, and why this approach is correct.

### 2.6 — File Issues for Unrelated Problems

During investigation you will often discover bugs, code smells, convention violations, or
safety concerns that are **not related** to the issue being resolved. **Do not ignore them.**

For each unrelated problem discovered:

1. **Assess severity:** Is it a correctness bug, a safety risk (data corruption, mount
   leak), a convention violation, or a latent defect?
2. **File a new GitHub issue** with a clear title, description of the problem, file/line
   references, and the context in which it was discovered.
3. **Add to the root-cause comment** (Phase 3) a "Related issues filed" section listing the
   new issue numbers and a one-line summary of each.
4. **Do not fix unrelated issues in the current PR** unless they are trivially small (e.g.,
   a typo on the same line you're already editing).

**Important distinction:** A convention violation in code that **your change modifies or
creates** is not "unrelated" — it is part of your change and must be fixed in this PR.
Only violations in code you did **not touch** qualify as "unrelated" and should be filed
as separate issues.

Examples of problems that **must** be filed:
- Missing `nsenter` wrapper on mount/umount operations
- Hardcoded paths instead of config.yaml values
- Missing `before_request` image-gate on a blueprint that requires a disk image
- Subprocess calls using `shell=True` or f-string command construction with user input
- Missing cleanup/restore paths in quick_edit operations
- Absent `fsync` on atomic file writes in vehicle (power-loss safety)

---

## Phase 3 — Post Root-Cause Comment

Post a comment on the GitHub issue documenting the root-cause analysis. This happens
**before** any code changes, so there is a record of the diagnosis.

### Comment format for bugs

```markdown
## Root Cause Analysis

**Symptom:** [Brief description of the observable problem]

**Root Cause:** [Clear statement of what is wrong and why]

**Location:** `path/to/file.py` L42-L55

**Evidence:**
- [Specific evidence point 1 with file/line references]
- [Specific evidence point 2]

**Eliminated hypotheses:**
- [Hypothesis 1] — eliminated because [evidence]
- [Hypothesis 2] — eliminated because [evidence]

**Planned fix:** [Brief description of what will be changed]

<!-- skill:resolve-issue:root-cause:{number} -->
```

### Comment format for features

```markdown
## Implementation Plan

**Feature:** [Brief description of what will be implemented]

**Approach:** [Clear statement of the implementation strategy and why it was chosen]

**Affected files:**
- `path/to/file.py` — [what changes]
- `path/to/new_file.py` — [new file, purpose]

**Key design decisions:**
- [Decision 1 and rationale]
- [Decision 2 and rationale]

<!-- skill:resolve-issue:implementation-plan:{number} -->
```

### Posting the comment

Before posting, check for duplicate comments:

```bash
gh issue view <number> --repo mphacker/TeslaUSB --json comments --jq '.comments[].body' | grep -F "skill:resolve-issue:root-cause:<number>"
```

If the marker is found, do not post a duplicate. If the user explicitly asks to re-analyze,
use an updated marker: `<!-- skill:resolve-issue:root-cause:{number}:updated -->`.

Post the comment:

```bash
gh issue comment <number> --repo mphacker/TeslaUSB --body-file "$TEMP/root-cause-<number>.md"
```

---

## Phase 3.5 — Create Feature Branch

Before making any code changes, create a new branch off `main`. All work for this issue
must happen on this branch — **never commit directly to `main`**.

```bash
git checkout main
git pull origin main

# Create and switch to a new branch
# Format: <type>/<issue-number>-<short-description>
# e.g., fix/42-video-streaming-timeout, feat/15-map-based-video-browser
# For sub-item mode: <type>/<issue-number>-item-<itemId>-<short-description>
git checkout -b <type>/<issue-number>-<short-description>
```

Where `<type>` matches the commit type (`fix`, `feat`, `refactor`, `docs`, `chore`) and
`<short-description>` is a brief kebab-case summary derived from the issue title.

---

## Phase 4 — Implementation

Implement the fix or feature following all project conventions from
`.github/copilot-instructions.md`.

### 4.1 — Key Conventions to Follow

Before writing code, review these TeslaUSB-specific rules:

| Convention | Rule |
|-----------|------|
| **Configuration** | Read all values from `config.yaml` via `config.sh` (Bash) or `config.py` (Python). Never hardcode paths, ports, or credentials. |
| **Mount safety** | All mount/umount/mountpoint commands must use `nsenter --mount=/proc/1/ns/mnt`. |
| **Atomic writes** | Use temp file + fsync + rename for any file write (power can drop at any time). |
| **Mode awareness** | Check current mode (present/edit) before file operations. Use `partition_service.get_mount_path()` for correct paths. |
| **Image gating** | New features depending on a disk image must have `@bp.before_request` guards and nav item guards in `base.html`. |
| **Template placeholders** | Use `__GADGET_DIR__`, `__MNT_DIR__`, `__TARGET_USER__` etc. in templates. Never hardcode installed paths. |
| **Subprocess safety** | Use list-based arguments (never `shell=True` with user input). |
| **Error handling** | Degrade gracefully — return empty data and log warnings instead of crashing. |
| **Resource constraints** | Optimize for Pi Zero 2 W (512MB RAM, 4-core ARM). Lazy-load heavy libraries, limit concurrency. |
| **Quick-edit cleanup** | Always restore RO mount and gadget LUN backing on all code paths (including error paths). |

### 4.2 — Implement the Fix/Feature

Make the minimum necessary code changes to resolve the issue. Follow all coding conventions
and architecture rules defined in `.github/copilot-instructions.md`.

**For Python changes:**
- Follow existing code style (PEP 8, consistent with adjacent code)
- Add logging at appropriate levels (WARNING for recoverable errors, ERROR for failures)
- Use Flask blueprint/service separation (routes in blueprints, logic in services)
- Handle both present and edit mode mount paths

**For Bash script changes:**
- Use `set -euo pipefail` at script top
- Quote all variable expansions
- Use `nsenter` for mount operations
- Add `sync` before and after critical filesystem operations

**For template/HTML changes:**
- Use Jinja2 template syntax consistently
- Wrap feature-dependent nav items in `{% if <flag> %}` guards
- Test with mobile-responsive layout

**For config.yaml changes:**
- Add new values with sensible defaults
- Document new config keys in the relevant section
- Update both `config.sh` and `config.py` wrappers if the value is used in both Bash and Python

### 4.3 — Verify Changes

Since TeslaUSB has no automated test suite, verification is manual:

1. **Syntax check Python files:**

   ```bash
   python3 -m py_compile scripts/web/<changed_file>.py
   ```

2. **Syntax check Bash scripts:**

   ```bash
   bash -n scripts/<changed_script>.sh
   ```

3. **Check for import errors** (if Flask app changes were made):

   ```bash
   cd scripts/web && python3 -c "from web_control import app; print('App loads OK')"
   ```

4. **Review all changed files** for:
   - Missing `nsenter` on mount operations
   - Hardcoded paths that should use config values
   - Missing cleanup/restore in error paths
   - Unsanitized user input in subprocess calls or file paths
   - Missing image-gate guards on new blueprints

### 4.4 — Template Deployment Check

If any files under `scripts/` or `templates/` were changed, remind the user:

> **Note:** After merging, run `sudo ./setup_usb.sh` on the Pi to deploy updated
> templates and scripts, then restart affected services
> (`sudo systemctl restart gadget_web.service`).

---

## Phase 5 — Self Code Review

After implementation, perform a code review of the changes. Since TeslaUSB does not have a
separate code-review skill, apply these checks inline.

### Review checklist

Review **only the changed files** against these TeslaUSB-specific criteria:

| Area | What to check |
|------|--------------|
| **Config usage** | No hardcoded paths/ports/credentials; all values from config.yaml via wrappers |
| **Mount safety** | All mount/umount wrapped in `nsenter --mount=/proc/1/ns/mnt`; sync before/after |
| **Mode awareness** | Correct mount paths for present (RO at `part*-ro`) vs edit (RW at `part*`) mode |
| **Path safety** | User-supplied filenames use `os.path.basename()`; resolved paths checked with `os.path.commonpath()` |
| **Subprocess safety** | List-based arguments; no `shell=True` with user input; no f-string command construction |
| **Image gating** | New features have `@bp.before_request` guards and `base.html` nav guards |
| **Atomic writes** | File writes use temp + fsync + rename pattern (power-loss safety) |
| **Quick-edit cleanup** | All code paths (including exceptions) restore RO mount and LUN backing |
| **Error handling** | Graceful degradation; appropriate logging; no bare `except:` without logging |
| **Resource efficiency** | No unbounded memory allocation; lazy imports for heavy libraries (av, PIL) |
| **Blueprint/service separation** | Business logic in services, not in route handlers |
| **Template placeholders** | New scripts/templates use `__GADGET_DIR__` etc., not hardcoded paths |

### Severity levels

| Severity | Meaning | Action |
|----------|---------|--------|
| 🔴 **Critical** | Mount safety violations, data corruption risk, command injection, path traversal | Must fix before proceeding |
| 🟡 **Warning** | Convention violations, missing error handling, hardcoded values | Should fix in this PR |
| 🔵 **Info** | Style suggestions, minor improvements | Fix if trivial, otherwise note |

Address all 🔴 Critical and 🟡 Warning findings before proceeding.

---

## Phase 6 — Post Resolution Comment

After the implementation passes self-review, post a comment on the GitHub issue summarizing
what was done.

### Comment format for bug fixes

```markdown
## Resolution

**Fix:** [Brief description of what was changed and why it resolves the root cause]

**Changes:**
- `path/to/file.py` — [what was changed and why]
- `path/to/file2.sh` — [what was changed and why]

**Verification:** Syntax checks pass; app loads without import errors; self code review
passed with no Critical or Warning findings.

<!-- skill:resolve-issue:resolution:{number} -->
```

### Comment format for features

```markdown
## Resolution

**Implemented:** [Brief description of the new functionality]

**Changes:**
- `path/to/file.py` — [what was added/modified]
- `path/to/new_file.py` — [new file, purpose]

**Verification:** Syntax checks pass; app loads without import errors; self code review
passed with no Critical or Warning findings.

<!-- skill:resolve-issue:resolution:{number} -->
```

### Posting the comment

Check for duplicates before posting:

```bash
gh issue view <number> --repo mphacker/TeslaUSB --json comments --jq '.comments[].body' | grep -F "skill:resolve-issue:resolution:<number>"
```

Post the comment:

```bash
gh issue comment <number> --repo mphacker/TeslaUSB --body-file "$TEMP/resolution-<number>.md"
```

### Sub-item mode — Mark item completed in issue body

After posting the resolution comment, update the issue body to mark the resolved item as
completed using strikethrough. This provides visual progress tracking directly in the issue.

**Step 1 — Fetch the current issue body.**
**Step 2 — Apply strikethrough** to the item's bold ID (table row: `| **{id}** |` →
`| ~~**{id}**~~ |`; checkbox: `- [ ]` → `- [x]`).
**Step 3 — Update the issue body** via `gh api`.
**Step 4 — Verify** the update was applied.

**Do not close the issue.** Other items remain to be resolved. Use `Ref #N` (not
`Closes #N`) in the commit message for sub-item mode.

---

## Phase 7 — Commit & Push Branch

Create a commit with a message that follows the project's commit conventions:

```
<type>: <description>

- <change detail 1>
- <change detail 2>

Closes #N

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

Where `<type>` is one of: `feat`, `fix`, `refactor`, `docs`, `chore`.

- **Single/filtered/all mode:** Use `Closes #N` to auto-close the issue when the PR is merged.
- **Sub-item mode:** Use `Ref #N` (not `Closes #N`) — the issue must remain open for
  remaining items. Include the item ID in the description: `Ref #N (item {id})`.

After committing, push the feature branch to the remote:

```bash
git push origin <branch-name>
```

**Do NOT push directly to `main`.** All changes must be on a feature branch and merged
via a pull request.

**Do NOT push** unless explicitly told to push to remote.

---

## Multi-Issue Processing

When resolving multiple issues (filtered or all mode):

1. Process each issue **sequentially** through Phases 2–7.
2. Track progress using todos or notes.
3. Each issue gets its own commit with its own `Closes #N` reference.
4. If issues are related or overlap, note cross-references in commit messages using `Ref #N`.
5. If an issue cannot be resolved (insufficient information, external dependency, etc.),
   post a comment explaining why and skip to the next issue.

---

## Guardrails

### What this skill does

- Reads GitHub issues via `gh` CLI
- Performs rigorous root-cause analysis with evidence and elimination
- **Files new GitHub issues for unrelated problems discovered during investigation**
- Posts diagnostic comments on issues before implementing
- Creates a feature branch off `main` before any code changes
- Implements fixes/features following all TeslaUSB conventions from `copilot-instructions.md`
- Performs self code review against TeslaUSB-specific criteria
- Posts resolution comments on issues documenting what was changed
- Creates properly-formatted commits with issue references on the feature branch
- Marks resolved sub-items in the issue body without closing the issue

### What this skill does NOT do

- **Does not commit or push to `main`** — all changes go on a feature branch.
- **Does not close issues manually** — relies on `Closes #N` in commit messages (except
  sub-item mode, which uses `Ref #N`).
- **Does not skip root-cause analysis** — even for "obvious" bugs, the analysis must be
  documented with evidence.
- **Does not ignore unrelated problems** — any bugs, safety violations, or risks discovered
  during investigation are filed as new GitHub issues (Phase 2.6).
- **Does not deploy to the Pi** — implementation and commits happen locally; deployment
  via `setup_usb.sh` is a separate manual step after merge.

### Quality gates

| Phase | Gate |
|-------|------|
| Phase 2 | All plausible hypotheses investigated with evidence; single root cause confirmed |
| Phase 3 | Comment posted on issue with root-cause analysis or implementation plan |
| Phase 3.5 | Feature branch created off `main`; working tree is on the new branch |
| Phase 4 | Python syntax checks pass; app loads without import errors |
| Phase 5 | No Critical or Warning findings from self code review |
| Phase 6 | Resolution comment posted on issue |
| Phase 7 | Commit created on feature branch with proper message format and issue reference |
