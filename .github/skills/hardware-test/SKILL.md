---
name: hardware-test
description: >
  Run an H-series increment on the live B-1 hardware target
  (`cybertruckusb.local`, user `pi`) using the mandatory safety
  wrapper: dead-man reboot timer, SSH liveness checks, file
  backups, idempotent operations. Use when asked to run a
  hardware smoke test, deploy a B-1 binary to the Pi, run an
  H-series increment (H0/H1/H2/H3/H4/H4c/H5/H6/H7), verify a
  hardware change, capture diagnostics from the device, or do
  any work that requires the live device to be touched. NEVER
  use this skill for v1 production work — it is B-1-only and
  assumes the device runs (or is being decommissioned to) the
  B-1 architecture. Refuses to proceed without explicit operator
  confirmation when the action could affect SSH, WiFi, or boot.
---

# Hardware Test on `cybertruckusb.local`

This skill is the single sanctioned way to interact with the
B-1 hardware target. Every other approach (raw `ssh`, raw
`scp`, ad-hoc `sshpass`, custom Python paramiko scripts) is
forbidden because they bypass the safety net.

**Operator directives this enforces (binding):**

> *"You will use the device at cybertruckusb.local (login with
> the account pi) for testing... You do need to be very careful
> to not knock it offline (break wifi connection), cause boot
> issue, or cause anything that would block you from SSH into
> the device."* — 2026-05-19

> *"Don't do a ton of work and wait to do code reviews. Have
> specific code review breaks and then fix ALL issues you find."*
> — 2026-05-19

**Second opinion before risky actions (binding).** When this skill
is reached as the culmination of *solving an issue* (a deploy that
fixes a bug, a diagnostic to root-cause a hardware fault), follow the
parallel GPT-5.5 second-opinion workflow in
`.github/copilot-instructions.md`: have a GPT-5.5 agent independently
research the problem, reconcile its view with yours into a single root
cause + plan, and **submit the deploy/diagnostic plan to the GPT-5.5
agent for review before touching the live device**. Live-hardware and
recording-critical actions must not proceed on a single (your own)
opinion alone.

The three sacred rails:

1. **SSH must stay up.** If it goes down between two of our
   commands, we cannot recover the device remotely. The
   operator would have to physically remove the Pi from the
   Cybertruck.
2. **WiFi must stay up.** SSH rides over WiFi. Any
   NetworkManager / wpa_supplicant change is GUARDED by a
   dead-man reboot.
3. **Boot must succeed.** Edits to `/boot/firmware/cmdline.txt`,
   `/boot/firmware/config.txt`, `/etc/fstab`, or kernel modules
   require a `.b1-backup-<timestamp>` sibling created BEFORE
   the edit, AND a dead-man reboot armed BEFORE the change is
   written.

---

## Phase 0 — Prerequisites

### Confirm the target

```bash
echo "B1_TARGET_HOST=${B1_TARGET_HOST:-cybertruckusb.local}"
echo "B1_TARGET_USER=${B1_TARGET_USER:-pi}"
```

If the user hasn't confirmed the target this session, ask:

> "Hardware test will target `cybertruckusb.local` (user `pi`).
> Confirm to proceed, or specify an alternate host."

### Verify known_hosts

```bash
ssh-keygen -F "${B1_TARGET_HOST:-cybertruckusb.local}"
```

If this returns empty, **stop and ask the operator** to
manually `ssh pi@cybertruckusb.local` once to record the host
key. Auto-accepting a host key is a security failure.

### Verify SSH key auth (no password prompts)

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${B1_TARGET_USER:-pi}@${B1_TARGET_HOST:-cybertruckusb.local}" \
    'echo SSH_OK'
```

If this prompts for a password or fails, stop and ask the
operator to set up SSH key auth. Password-prompt SSH is
incompatible with the dead-man wrapper.

### Confirm baseline alive

```bash
ssh ... 'uptime && systemctl is-system-running'
```

Capture the baseline. Any later check shows degradation, we
have evidence.

---

## Phase 1 — Scope resolution

Determine which H-series increment (per `docs/00-PLAN.md`
"Phased implementation") is being run. The user will name one,
e.g. "H0", "H1.4", "H4c.5", "H5.b". Refuse to run if:

- The increment number does not appear in `00-PLAN.md`.
- The increment's predecessor in the plan has not been
  completed (check `01-PROGRESS.md` for the predecessor's
  ✅ mark).
- Any 🔍 REVIEW GATE between the predecessor and this
  increment has not produced a charter-review report
  marked APPROVED.

This skill is part of the gate enforcement, not separate from
it.

---

## Phase 2 — Arm the dead-man switch

Before any potentially-disruptive remote command, arm a
self-rebooting timer on the device. If our test wedges or
locks us out, the device reboots itself in 3 minutes.

```bash
ssh ... bash <<'EOF'
sudo systemd-run --on-active=180 --unit=b1-deadman \
    --description="B-1 hardware-test dead-man switch (3 min)" \
    /sbin/reboot
echo "Dead-man armed at $(date -Is); device will reboot in 180s if not cancelled"
EOF
```

Record the start time. The skill's internal timer must
remember to cancel the dead-man if the test completes:

```bash
ssh ... 'sudo systemctl stop b1-deadman.timer 2>/dev/null; sudo systemctl reset-failed b1-deadman.timer 2>/dev/null; echo dead-man-cancelled'
```

**If the test takes > 150 seconds**, re-arm the dead-man
periodically (every 120 seconds, e.g., between sub-steps).
Better to re-arm too often than miss a wedge.

---

## Phase 3 — Snapshot first, mutate after

For any step that modifies files outside our own
`/home/pi/teslausb-b1/` sandbox, snapshot first:

```bash
ssh ... bash <<EOF
sudo cp -a /etc/systemd/system/gadget_web.service \
          /etc/systemd/system/gadget_web.service.b1-backup-\$(date +%Y%m%d-%H%M%S)
EOF
```

For `~/TeslaUSB` v1 decommissioning specifically (Phase H0.1):

```bash
ssh ... 'sudo tar -czf /home/pi/v1-backup-$(date +%Y%m%d).tar.gz \
    --exclude=/home/pi/ArchivedClips \
    /etc/ /home/pi/TeslaUSB 2>&1 | tail -30'
scp pi@cybertruckusb.local:/home/pi/v1-backup-*.tar.gz \
    ~/.copilot/session-state/<sid>/files/
ssh ... 'sha256sum /home/pi/v1-backup-*.tar.gz'
sha256sum ~/.copilot/session-state/<sid>/files/v1-backup-*.tar.gz
```

Sha256 sums MUST match. Otherwise the `scp` was corrupt;
retry.

---

## Phase 4 — Execute the increment

For each sub-step in the increment's table from `00-PLAN.md`:

1. Print the sub-step description to the user.
2. Run the command via the SSH wrapper:
   ```bash
   ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
       -o ConnectTimeout=10 -o BatchMode=yes \
       pi@cybertruckusb.local \
       'set -euo pipefail; <command>'
   ```
3. **Liveness check after every sub-step:**
   ```bash
   ssh ... 'echo alive && uptime'
   ```
   If this fails:
   - Wait 30 seconds (transient network blip).
   - Retry once.
   - If still failing, STOP. Wait for the dead-man reboot
     (180 seconds from arming). Try again.
   - If still failing after dead-man, **escalate to operator
     immediately** with the full transcript. Do not attempt
     further commands.
4. Log every command + exit code + (truncated) stdout/stderr
   to `~/.copilot/session-state/<sid>/files/hw-<increment>-<timestamp>.log`.
5. Re-arm dead-man if elapsed > 120 s since last arm.

---

## Phase 5 — Verify post-conditions

Each increment in `00-PLAN.md` has an explicit "Verify after"
column. Each one MUST pass before the increment is marked done.
Common verifications:

| Verification | Command |
|---|---|
| SSH alive | `ssh ... 'echo alive'` |
| WiFi alive | `ssh ... 'nmcli -t -f STATE general && nmcli -t -f DEVICE,STATE device | grep wlan0'` |
| Boot success | `ssh ... 'systemctl is-system-running'` (`running` or `degraded` OK; `maintenance` = bad) |
| Service expected state | `ssh ... 'systemctl is-active <unit>; systemctl is-enabled <unit>'` |
| Free space reclaimed | `ssh ... 'df -h /home/pi'` |
| Specific file present | `ssh ... 'sha256sum <path>'` |
| Specific file ABSENT | `ssh ... '! test -e <path> || (echo FAIL: file still present; exit 1)'` |
| Process running | `ssh ... 'pgrep -f <binary> >/dev/null && echo running'` |
| Process NOT running | `ssh ... '! pgrep -f <binary>'` |

If ANY verification fails, the increment is FAILED. Do not
mark it done. Stop and report. Do not attempt the next
increment.

---

## Phase 6 — Cancel dead-man + capture journal

```bash
ssh ... 'sudo systemctl stop b1-deadman.timer 2>/dev/null; echo dead-man-cancelled'
ssh ... 'sudo journalctl --since "10 min ago" --no-pager' \
    > ~/.copilot/session-state/<sid>/files/hw-<increment>-journal-<timestamp>.log
```

---

## Phase 7 — Report

Append to `~/.copilot/session-state/<sid>/files/hw-results.md`:

```markdown
## Hardware test: <increment>

- **Started:** <ISO>
- **Ended:** <ISO>
- **Target:** cybertruckusb.local (pi)
- **Result:** PASS / FAIL

### Sub-steps
| # | Description | Status | Notes |
|---|---|---|---|
| H0.1 | snapshot | ✅ | sha256 match |
| H0.2 | stop services | ✅ | all 6 stopped cleanly |
| ... | | | |

### Verifications
| Check | Result |
|---|---|
| SSH alive (final) | ✅ |
| WiFi alive (final) | ✅ |
| Boot status | running |
| Journal contains no error | ✅ |
| Specific post-conditions | ✅ |

### Logs
- Command log: `hw-<increment>-<timestamp>.log`
- Journal: `hw-<increment>-journal-<timestamp>.log`
- Snapshots: `<list>`

### Next action
- [✅ if PASS] Update `docs/01-PROGRESS.md`, mark increment done, proceed to next 🔍 REVIEW GATE.
- [❌ if FAIL] STOP. Surface failure to operator. Do not run further increments.
```

If PASS, then surface to user: "H<n> complete. The next step
is the charter-review gate on this increment. Shall I invoke
the `charter-review` skill on `scope: increment H<n>`?"

If FAIL, surface to user with full transcript and **do not
suggest a workaround**. The operator decides whether to
rollback (via the `.b1-backup` snapshots and the `v1-backup`
tarball) or investigate.

---

## Refuse-to-proceed conditions

Hard stops where the skill SHALL NOT do the operation:

- The increment touches `/etc/ssh/`, `/etc/NetworkManager/`,
  `/etc/wpa_supplicant/`, `/etc/sudoers`, or
  `/etc/systemd/system/sshd*` AND operator has not explicitly
  confirmed the change in THIS session's prior turn.
- The increment proposes to edit `/boot/firmware/cmdline.txt`
  or `config.txt` without a `.b1-backup-<timestamp>` step
  immediately preceding it.
- The increment proposes to run `systemctl mask` without first
  running `systemctl disable` and rebooting (two-stage
  reversibility — disable lets us re-enable, mask is harder
  to recover from accidentally).
- The increment proposes to `rm -rf` outside `/home/pi/teslausb-b1/`
  or `/home/pi/v1-backup-*` without explicit operator
  confirmation in the prior turn.
- The dead-man wrapper isn't installed (cannot arm timer).
- Three consecutive sub-steps have logged SSH retry warnings —
  the network is unreliable, stop before something breaks.

In all these cases: surface the refusal with the specific rule
that blocked, ask the operator to either confirm or amend the
plan, then resume only after explicit confirmation.

---

## Notes on testing across phases

| Phase H-series | Frequency in plan |
|---|---|
| H0 | once, before any other H |
| H1 | once, after Phase 1 increments complete + charter-review approved |
| H2 | once per phase 2 batch (~ every 4 increments) |
| H3 | once after phase 3 increments |
| H4 | once after phase 4 + 4b increments |
| H4c | once after phase 4c increments |
| H5.a, H5.b, ... | every 3 phase 5 increments (UI screenshot diff) |
| H6 | once after phase 6 increments (on second Pi, or on the live device with a clean SD card) |
| H7 | the soak runs — 24 h then 72 h |

Plan-level rule: NO H-series increment runs without ALL
non-hardware predecessors having a green charter-review +
green automated tests on the dev box.
